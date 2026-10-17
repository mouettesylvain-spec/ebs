"""Retry policy for infrastructure failures (invariant I12, P0-15 R6, P1-05).

Infrastructure failures (license timeout, OOM, node failure, preemption, runner crash) are never
cached; the driver asks a `RetryPolicy` whether to try again. A decision may also change the
action's resources (more memory after an OOM), which never changes its key, and may ask that the
next attempt avoids the node of the failed one. A test failure is a result, not an
infrastructure failure: the policy is never asked about it (R5).

`ReasonRetry` is the per-reason policy of `[retry]` (P1-05 R1):

- `oom`: memory x `oom_mem_factor`, rounded up to a MiB and capped at `max_mem`. Without a
  memory request, or already at the cap, the OOM is final (it would happen again).
- `timeout`: final (usually a hang) unless `resources.retry_on_timeout` says otherwise (default
  `[retry].retry_on_timeout`); never cached either way.
- `node_fail`, `preempted`, `runner_crash`, `other`: same resources after the backoff
  (`backoff_s` x `backoff_factor`^(attempt-1), at most `backoff_max_s`).
- `license`: same resources after `license_backoff_s` x attempt.
- `input_verification` (runner exit 76: staged bytes did not match their digest, from CAS
  corruption or a stale NFS view): `input_verification_retries` times (default once), each
  avoiding the node of the failed attempt; then final, with a hint to check the CAS.

Every reason is bounded by `max_retries`. `BackoffRetry` (the P0 policy: every reason alike)
stays the default of `run_build`; `ebs build` uses `ReasonRetry`.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol, cast, get_args

from ebs.core.errors import ConfigError
from ebs.core.log import get_logger
from ebs.flow.model import Resources, parse_memory
from ebs.meta.api import Event, InfraReason
from ebs.plan.types import ActionSpec

__all__ = [
    "Attempt",
    "BackoffRetry",
    "ReasonRetry",
    "RetryDecision",
    "RetryPolicy",
    "RetrySettings",
    "attempt_history",
]

_log = get_logger(__name__)

_MIB: Final = 1 << 20
_INFRA_REASONS: Final[frozenset[str]] = frozenset(get_args(InfraReason))


@dataclass(frozen=True, slots=True)
class RetryDecision:
    delay_s: float  # wait before resubmitting
    resources: Resources | None = None  # replaces the action's resources for the next attempt
    avoid_node: bool = False  # the next attempt must not run on the failed attempt's node


class RetryPolicy(Protocol):
    def decide(
        self,
        spec: ActionSpec,
        attempt: int,
        reason: InfraReason,
        earlier: tuple[InfraReason, ...] = (),
    ) -> RetryDecision | None:
        """Retry after `attempt` failed attempts (1 = the first try failed), or None to give up.

        `earlier` are the reasons of the action's previous failed attempts, oldest first
        (P1-05: a policy may bound one reason on its own)."""
        ...


@dataclass(frozen=True, slots=True)
class BackoffRetry:
    """Up to `max_retries` retries, waiting base_s, base_s * factor, … (at most max_s)."""

    max_retries: int = 2
    base_s: float = 10.0
    factor: float = 2.0
    max_s: float = 300.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ConfigError(f"max_retries must be >= 0, got {self.max_retries}")
        if self.base_s < 0 or self.max_s < 0:
            raise ConfigError(
                f"retry delays must be >= 0 seconds, got base_s={self.base_s}, max_s={self.max_s}"
            )
        if self.factor < 1:
            raise ConfigError(f"retry backoff factor must be >= 1, got {self.factor}")

    def decide(
        self,
        spec: ActionSpec,
        attempt: int,
        reason: InfraReason,
        earlier: tuple[InfraReason, ...] = (),
    ) -> RetryDecision | None:
        del spec, reason, earlier  # every infrastructure reason is retried the same way in P0
        if attempt > self.max_retries:
            return None
        return RetryDecision(delay_s=min(self.base_s * self.factor ** (attempt - 1), self.max_s))


# --- [retry] -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetrySettings:
    """`[retry]` in the site config (P1-05 R1); see the module docstring for the semantics."""

    max_retries: int = 2
    backoff_s: float = 10.0
    backoff_factor: float = 2.0
    backoff_max_s: float = 300.0
    oom_mem_factor: float = 1.5
    max_mem: int | None = None  # bytes; None: no cap
    license_backoff_s: float = 60.0
    retry_on_timeout: bool = False  # default for steps that do not say
    input_verification_retries: int = 1

    def __post_init__(self) -> None:
        for name in ("max_retries", "input_verification_retries"):
            if getattr(self, name) < 0:
                raise ConfigError(f"[retry].{name} must be >= 0, got {getattr(self, name)}")
        for name in ("backoff_s", "backoff_max_s", "license_backoff_s"):
            if getattr(self, name) < 0:
                raise ConfigError(f"[retry].{name} must be >= 0 seconds, got {getattr(self, name)}")
        if self.backoff_factor < 1:
            raise ConfigError(f"[retry].backoff_factor must be >= 1, got {self.backoff_factor}")
        if self.oom_mem_factor <= 1:
            raise ConfigError(
                f"[retry].oom_mem_factor must be > 1 (the retry needs more memory), "
                f"got {self.oom_mem_factor}"
            )
        if self.max_mem is not None and self.max_mem < _MIB:
            raise ConfigError(f"[retry].max_mem must be at least 1M, got {self.max_mem} bytes")

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> RetrySettings:
        section = config.get("retry", {})
        if not isinstance(section, Mapping):
            raise ConfigError(f"[retry] must be a table, got {section!r}")
        known = set(cls.__dataclass_fields__)
        unknown = sorted(str(k) for k in section if k not in known)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [retry]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(known))}"
            )
        values: dict[str, object] = {}
        for key, raw in section.items():
            if key in ("max_retries", "input_verification_retries"):
                values[key] = _int(raw, key)
            elif key == "retry_on_timeout":
                if not isinstance(raw, bool):
                    raise ConfigError(
                        f"[retry].retry_on_timeout must be true or false, got {raw!r}"
                    )
                values[key] = raw
            elif key == "max_mem":
                values[key] = _memory(raw)
            else:
                values[key] = _number(raw, str(key))
        return cls(**values)  # type: ignore[arg-type]


def _int(raw: object, key: str) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise ConfigError(f"[retry].{key} must be an integer, got {raw!r}")
    return raw


def _number(raw: object, key: str) -> float:
    if not isinstance(raw, int | float) or isinstance(raw, bool):
        raise ConfigError(f"[retry].{key} must be a number, got {raw!r}")
    return float(raw)


def _memory(raw: object) -> int:
    if not isinstance(raw, str):
        raise ConfigError(f"[retry].max_mem must be a memory size such as '256G', got {raw!r}")
    try:
        return parse_memory(raw)
    except ValueError as exc:
        raise ConfigError(f"[retry].max_mem: {exc}") from None


# --- the per-reason policy ---------------------------------------------------------------------


def grown_memory(mem: int, factor: float, cap: int | None) -> int | None:
    """Memory for the attempt after an OOM: `mem` x `factor`, rounded up to a MiB, at most `cap`
    (rounded down to a MiB, as SLURM's --mem takes MiB); None when it cannot grow (at the cap)."""
    grown = math.ceil(mem * factor / _MIB) * _MIB
    if cap is not None:
        grown = min(grown, cap // _MIB * _MIB)
    return grown if grown > mem else None


@dataclass(frozen=True, slots=True)
class ReasonRetry:
    """Retries each infrastructure reason its own way (P1-05 R1)."""

    settings: RetrySettings = RetrySettings()

    def decide(
        self,
        spec: ActionSpec,
        attempt: int,
        reason: InfraReason,
        earlier: tuple[InfraReason, ...] = (),
    ) -> RetryDecision | None:
        s = self.settings
        if attempt > s.max_retries:
            return None
        backoff = min(s.backoff_s * s.backoff_factor ** (attempt - 1), s.backoff_max_s)
        if reason == "oom":
            return self._oom(spec, backoff)
        if reason == "timeout":
            wanted = spec.resources.retry_on_timeout
            if not (s.retry_on_timeout if wanted is None else wanted):
                return None  # usually a hang: running it again would hang again
            return RetryDecision(delay_s=backoff)
        if reason == "license":
            return RetryDecision(delay_s=s.license_backoff_s * attempt)
        if reason == "input_verification":
            # Staged bytes did not match their digest: CAS corruption or a stale NFS view on
            # that node. Once more elsewhere; a second mismatch is not a node problem.
            if earlier.count("input_verification") >= s.input_verification_retries:
                return None
            return RetryDecision(delay_s=backoff, avoid_node=True)
        return RetryDecision(delay_s=backoff)  # node_fail, preempted, runner_crash, other

    def _oom(self, spec: ActionSpec, backoff: float) -> RetryDecision | None:
        mem = spec.resources.mem
        if not isinstance(mem, int):
            _log.warning(
                "driver.oom_without_memory_request",
                action=spec.action_id,
                hint="set resources.mem on the step so that a retry can ask for more",
            )
            return None
        grown = grown_memory(mem, self.settings.oom_mem_factor, self.settings.max_mem)
        if grown is None:
            _log.warning(
                "driver.oom_at_max_mem",
                action=spec.action_id,
                mem=mem,
                max_mem=self.settings.max_mem,
            )
            return None
        return RetryDecision(
            delay_s=backoff, resources=spec.resources.model_copy(update={"mem": grown})
        )


# --- attempt history (R2) ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Attempt:
    """One submission of an action, from the driver's events."""

    attempt: int
    job_id: str | None = None
    node: str | None = None  # where it failed, when the executor knows
    reason: InfraReason | None = None  # None: not an infrastructure failure (or not over yet)

    def to_json(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "job_id": self.job_id,
            "node": self.node,
            "reason": self.reason,
        }


def attempt_history(events: Iterable[Event]) -> dict[str, list[Attempt]]:
    """Every attempt of every submitted action, in order, from the driver's events
    (`submitted` and `infra_failed`, both numbered by `attempt`)."""
    found: dict[str, dict[int, Attempt]] = {}
    for event in events:
        action_id = event.action_id
        number = event.data.get("attempt")
        if action_id is None or not isinstance(number, int) or isinstance(number, bool):
            continue  # e.g. the runner's own infra_failed event, which has no attempt number
        attempts = found.setdefault(action_id, {})
        current = attempts.get(number, Attempt(number))
        if event.type == "submitted":
            job = event.data.get("job_id")
            attempts[number] = Attempt(
                number, str(job) if job is not None else None, current.node, current.reason
            )
        elif event.type == "infra_failed":
            reason = event.data.get("reason")
            node = event.data.get("node")
            attempts[number] = Attempt(
                number,
                current.job_id,
                node if isinstance(node, str) else None,
                cast(InfraReason, reason) if reason in _INFRA_REASONS else "other",
            )
    return {a: [attempts[n] for n in sorted(attempts)] for a, attempts in found.items()}
