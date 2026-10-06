"""Cache participation of a build (architecture.md "Cache semantics", P0-15 R2).

- `off`: never read or write the action cache.
- `read`: read, never write (default for personal builds, so they do not pollute CI's cache).
- `write`: read and write (CI and release builds).

The driver reads; writes are done by the runner, which `cache_put`s its result when the build's
cache mode is `write` (interfaces.md § 9). `rerun_failed` ignores cached test failures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from ebs.core.errors import ConfigError
from ebs.meta.api import CacheMode, ResultManifest

CACHE_MODES: tuple[str, ...] = get_args(CacheMode)


@dataclass(frozen=True, slots=True)
class CachePolicy:
    mode: CacheMode
    rerun_failed: bool = False

    def __post_init__(self) -> None:
        if self.mode not in CACHE_MODES:
            raise ConfigError(
                f"unknown cache mode {self.mode!r}; use one of {', '.join(CACHE_MODES)}"
            )

    @property
    def reads(self) -> bool:
        return self.mode != "off"

    @property
    def writes(self) -> bool:
        return self.mode == "write"

    def accept(self, result: ResultManifest) -> bool:
        """Whether a cache hit may be used instead of running the action."""
        return not (self.rerun_failed and result.status == "failed")
