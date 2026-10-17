"""Runner settings from ebs.toml: `[cas]`, `[scratch]`, `[debug]`, `[runner]`, `[rules]`."""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from ebs.cas.api import DEFAULT_COPY_THRESHOLD
from ebs.core.errors import ConfigError
from ebs.domains import Domains
from ebs.flow.model import parse_memory
from ebs.rules.api import DEFAULT_LICENSE_ERROR_PATTERNS

__all__ = [
    "DEFAULT_DEBUG_MAX_SIZE",
    "DEFAULT_HEARTBEAT_S",
    "DEFAULT_MAX_LOG",
    "DEFAULT_PASSTHROUGH_ENV",
    "RunnerSettings",
]

DEFAULT_PASSTHROUGH_ENV: Final[tuple[str, ...]] = ("LM_LICENSE_FILE", "*_LICENSE_FILE", "SLURM_*")
"""Caller variables the tool may see (case-sensitive globs). Site-specific, never in the key."""

DEFAULT_MAX_LOG: Final = 2 << 30  # 2 GiB
DEFAULT_KILL_GRACE_S: Final = 30.0
DEFAULT_DEBUG_MAX_SIZE: Final = 2 << 30  # 2 GiB, for steps whose `debug.max_size` is unset
DEFAULT_HEARTBEAT_S: Final = 60.0
_RUNNER_KEYS: Final = frozenset({"passthrough_env", "max_log", "kill_grace_s", "heartbeat_s"})


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    cas_root: Path
    scratch_dir: Path
    passthrough_env: tuple[str, ...] = DEFAULT_PASSTHROUGH_ENV
    max_log: int = DEFAULT_MAX_LOG  # bytes of tool log kept (the tail)
    kill_grace_s: float = DEFAULT_KILL_GRACE_S  # SIGTERM -> SIGKILL on timeout
    license_error_patterns: tuple[str, ...] = DEFAULT_LICENSE_ERROR_PATTERNS
    copy_threshold: int = DEFAULT_COPY_THRESHOLD  # larger inputs are linked read-only (P1-04)
    debug_root: Path | None = None  # None: no debug collection nor live log
    debug_max_size: int = DEFAULT_DEBUG_MAX_SIZE
    heartbeat_s: float = DEFAULT_HEARTBEAT_S  # lease renewal period while the tool runs
    domains: Domains = field(default_factory=lambda: Domains({}))  # P2-01: per-domain roots

    def cas_root_for(self, domain: str) -> Path:
        """The root holding `<domain>/` of the domain's CAS (`[[domains]]`, else [cas].root)."""
        return self.domains.cas_root(domain, self.cas_root)

    def debug_root_for(self, domain: str) -> Path | None:
        """The root holding `<domain>/` of the domain's debug area, or None."""
        return self.domains.debug_root(domain, self.debug_root)

    @classmethod
    def from_config(
        cls, config: Mapping[str, Mapping[str, object]], environ: Mapping[str, str]
    ) -> RunnerSettings:
        """Validate the sections the runner reads; ConfigError names the bad key."""
        cas = config.get("cas", {})
        root = cas.get("root")
        if not isinstance(root, str) or not root:
            raise ConfigError("[cas].root is not set: give the CAS root directory in ebs.toml")
        if not Path(root).is_absolute():
            raise ConfigError(f"[cas].root must be an absolute path, got {root!r}")
        scratch = config.get("scratch", {}).get("dir", "${TMPDIR}/ebs")
        if not isinstance(scratch, str):
            raise ConfigError(f"[scratch].dir must be a string, got {scratch!r}")
        try:
            scratch = string.Template(scratch).substitute({"TMPDIR": "/tmp", **environ})
        except (KeyError, ValueError) as exc:
            raise ConfigError(
                f"[scratch].dir {scratch!r} refers to {exc}, which is not set in the environment"
            ) from None

        runner = config.get("runner", {})
        unknown = sorted(set(runner) - _RUNNER_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [runner]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(_RUNNER_KEYS))}"
            )
        debug = config.get("debug", {})
        debug_root = debug.get("root")
        if debug_root is not None and (
            not isinstance(debug_root, str) or not Path(debug_root).is_absolute()
        ):
            raise ConfigError(f"[debug].root must be an absolute path, got {debug_root!r}")
        rules = config.get("rules", {})
        return cls(
            cas_root=Path(root),
            scratch_dir=Path(scratch),
            passthrough_env=_strings(
                runner.get("passthrough_env", DEFAULT_PASSTHROUGH_ENV), "[runner].passthrough_env"
            ),
            max_log=_size(runner.get("max_log", DEFAULT_MAX_LOG), "[runner].max_log"),
            kill_grace_s=_seconds(
                runner.get("kill_grace_s", DEFAULT_KILL_GRACE_S), "[runner].kill_grace_s"
            ),
            license_error_patterns=_strings(
                rules.get("license_error_patterns", DEFAULT_LICENSE_ERROR_PATTERNS),
                "[rules].license_error_patterns",
            ),
            copy_threshold=_size(
                cas.get("copy_threshold", DEFAULT_COPY_THRESHOLD), "[cas].copy_threshold"
            ),
            debug_root=None if debug_root is None else Path(debug_root),
            debug_max_size=_size(debug.get("max_size", DEFAULT_DEBUG_MAX_SIZE), "[debug].max_size"),
            heartbeat_s=_positive_seconds(
                runner.get("heartbeat_s", DEFAULT_HEARTBEAT_S), "[runner].heartbeat_s"
            ),
            domains=Domains.from_config(config),
        )


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings, got {value!r}")
    return tuple(value)


def _size(value: object, where: str) -> int:
    if isinstance(value, str):
        try:
            value = parse_memory(value)
        except ValueError:
            raise ConfigError(
                f"{where}: invalid size {value!r}; use bytes or e.g. '2GiB'"
            ) from None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{where} must be a positive size, got {value!r}")
    return value


def _seconds(value: object, where: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{where} must be a non-negative number of seconds, got {value!r}")
    return float(value)


def _positive_seconds(value: object, where: str) -> float:
    seconds = _seconds(value, where)
    if seconds == 0:
        raise ConfigError(f"{where} must be a positive number of seconds, got {value!r}")
    return seconds
