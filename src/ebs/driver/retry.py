"""Retry policy for infrastructure failures (invariant I12, P0-15 R6).

Infrastructure failures (license timeout, OOM, node failure, preemption, runner crash) are never
cached; the driver asks a `RetryPolicy` whether to try again. A decision may also change the
action's resources (P1-05: more memory after an OOM), which never changes its key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ebs.core.errors import ConfigError
from ebs.flow.model import Resources
from ebs.meta.api import InfraReason
from ebs.plan.types import ActionSpec


@dataclass(frozen=True, slots=True)
class RetryDecision:
    delay_s: float  # wait before resubmitting
    resources: Resources | None = None  # replaces the action's resources for the next attempt


class RetryPolicy(Protocol):
    def decide(self, spec: ActionSpec, attempt: int, reason: InfraReason) -> RetryDecision | None:
        """Retry after `attempt` failed attempts (1 = the first try failed), or None to give up."""
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

    def decide(self, spec: ActionSpec, attempt: int, reason: InfraReason) -> RetryDecision | None:
        del spec, reason  # every infrastructure reason is retried the same way in P0
        if attempt > self.max_retries:
            return None
        return RetryDecision(delay_s=min(self.base_s * self.factor ** (attempt - 1), self.max_s))
