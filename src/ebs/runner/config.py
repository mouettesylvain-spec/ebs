"""Runner settings from ebs.toml: `[cas]`, `[scratch]`, `[runner]`, `[rules]`."""

from __future__ import annotations

import string
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.core.errors import ConfigError
from ebs.flow.model import parse_memory
from ebs.rules.api import DEFAULT_LICENSE_ERROR_PATTERNS

__all__ = ["DEFAULT_MAX_LOG", "DEFAULT_PASSTHROUGH_ENV", "RunnerSettings"]

DEFAULT_PASSTHROUGH_ENV: Final[tuple[str, ...]] = ("LM_LICENSE_FILE", "*_LICENSE_FILE", "SLURM_*")
"""Caller variables the tool may see (case-sensitive globs). Site-specific, never in the key."""

DEFAULT_MAX_LOG: Final = 2 << 30  # 2 GiB
DEFAULT_KILL_GRACE_S: Final = 30.0
_RUNNER_KEYS: Final = frozenset({"passthrough_env", "max_log", "kill_grace_s"})


@dataclass(frozen=True, slots=True)
class RunnerSettings:
    cas_root: Path
    scratch_dir: Path
    passthrough_env: tuple[str, ...] = DEFAULT_PASSTHROUGH_ENV
    max_log: int = DEFAULT_MAX_LOG  # bytes of tool log kept (the tail)
    kill_grace_s: float = DEFAULT_KILL_GRACE_S  # SIGTERM -> SIGKILL on timeout
    license_error_patterns: tuple[str, ...] = DEFAULT_LICENSE_ERROR_PATTERNS

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
