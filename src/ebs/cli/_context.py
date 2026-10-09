"""What the commands need from the site, built from ebs.toml (one `SiteServices` per command).

Tests replace it with a subclass (in-memory store, scripted executor, fake clock) passed as the
Typer context object. Config sections read here: `[cas]`, `[scratch]`, `[metadata]`, `[rules]`,
`[stat_cache]` (docs/design/overview.md § Site configuration).
"""

from __future__ import annotations

import contextlib
import getpass
import os
import shutil
import stat
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.cas.fs import FsCAS
from ebs.config import Config, config_paths, load_config
from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError, ExecutorError, MetadataError, SourceError, ToolchainError
from ebs.exec.api import Executor
from ebs.exec.local import LocalExecutor
from ebs.meta.api import MetadataStore
from ebs.rules.api import DEFAULT_LICENSE_ERROR_PATTERNS, RuleSettings
from ebs.rules.registry import RuleRegistry
from ebs.runner.config import RunnerSettings
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import DEFAULT_RACY_WINDOW_S, StatCache, default_statcache_path
from ebs.toolchain.model import StaticToolchainResolver, Toolchain, ToolchainResolver

__all__ = ["EXECUTORS", "SiteServices", "StatCacheSettings"]

EXECUTORS: Final = ("local",)
TOOLCHAINS_FILE: Final = Path(".ebs") / "toolchains.yaml"
_STAT_KEYS: Final = frozenset({"path", "racy_window_s", "audit_fraction", "untrusted_mounts"})


@dataclass(frozen=True, slots=True)
class StatCacheSettings:
    """The `[stat_cache]` section."""

    path: Path | None = None  # None: default_statcache_path(uid)
    racy_window_s: float = DEFAULT_RACY_WINDOW_S
    audit_fraction: float = 0.01
    untrusted_mounts: tuple[Path, ...] = ()

    @classmethod
    def from_section(cls, section: Mapping[str, object]) -> StatCacheSettings:
        unknown = sorted(set(section) - _STAT_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [stat_cache]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(_STAT_KEYS))}"
            )
        path = section.get("path")
        if path is not None and (not isinstance(path, str) or not path):
            raise ConfigError(f"[stat_cache].path must be a non-empty string, got {path!r}")
        mounts = section.get("untrusted_mounts", [])
        if not isinstance(mounts, list) or not all(isinstance(m, str) for m in mounts):
            raise ConfigError(
                f"[stat_cache].untrusted_mounts must be a list of paths, got {mounts!r}"
            )
        return cls(
            path=Path(path).expanduser() if path else None,
            racy_window_s=_number(section, "racy_window_s", DEFAULT_RACY_WINDOW_S),
            audit_fraction=_number(section, "audit_fraction", 0.01),
            untrusted_mounts=tuple(Path(m) for m in mounts),
        )


def _number(section: Mapping[str, object], key: str, default: float) -> float:
    value = section.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"[stat_cache].{key} must be a non-negative number, got {value!r}")
    return float(value)


class _UnsafeDir(Exception):
    pass


def _private_dir(path: Path) -> None:
    """Create `path` mode 0700, or accept it only if it is ours and closed to others."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise _UnsafeDir(f"{path} is not a directory")
    if st.st_uid != os.getuid():
        raise _UnsafeDir(f"{path} belongs to uid {st.st_uid}, not to you")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise _UnsafeDir(
            f"{path} has mode {oct(stat.S_IMODE(st.st_mode))}; others could plant a database "
            "there (expected 0o700)"
        )


class _MissingToolchains:
    """Resolver for a flow without `.ebs/toolchains.yaml`: fine until a step names a toolchain."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def resolve(self, name: str, module: str) -> Toolchain:
        raise ToolchainError(
            f"toolchain {name!r} ({module}) cannot be resolved: {self._path} does not exist; "
            "create it with the toolchain's module, install roots and env"
        )


class SiteServices:
    """Config-driven factories for the CAS, metadata store, executor, rules and stat cache."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str],
        cwd: Path,
        home: Path,
        clock: Clock | None = None,
        statcache_default: Path | None = None,
    ) -> None:
        self.environ = dict(environ)
        self.cwd = cwd
        self.home = home
        self.clock: Clock = clock or SystemClock()
        self._statcache_default = statcache_default or default_statcache_path(os.getuid())
        self._config: Config | None = None
        self._store: MetadataStore | None = None

    @classmethod
    def from_process(cls) -> SiteServices:
        environ = dict(os.environ)
        return cls(environ=environ, cwd=Path.cwd(), home=Path(environ.get("HOME") or Path.home()))

    # --- config ---------------------------------------------------------------------------------

    def config(self) -> Config:
        if self._config is None:
            self._config = load_config(config_paths(self.environ, cwd=self.cwd, home=self.home))
        return self._config

    def _section(self, name: str) -> dict[str, object]:
        return self.config().get(name, {})

    def runner_settings(self) -> RunnerSettings:
        """`[cas]`, `[scratch]`, `[runner]`, `[rules]` as the runner reads them."""
        return RunnerSettings.from_config(self.config(), self.environ)

    def user(self) -> str:
        return self.environ.get("USER") or self.environ.get("LOGNAME") or getpass.getuser()

    def is_tty(self) -> bool:
        return sys.stdout.isatty()

    # --- storage --------------------------------------------------------------------------------

    def cas(self, domain: str) -> FsCAS:
        return FsCAS(self.runner_settings().cas_root, domain, clock=self.clock)

    def store(self, *, required: bool) -> MetadataStore | None:
        """The metadata store of `[metadata].url`; None (or ConfigError if required) without it."""
        if self._store is not None:
            return self._store
        section = self._section("metadata")
        if "url" not in section:
            if required:
                raise ConfigError(
                    "this command needs the metadata store: set [metadata].url in ebs.toml "
                    "(postgresql+psycopg://user@host/db)"
                )
            return None
        from ebs.meta.pg import PgMetadataStore  # SQLAlchemy only when a store is configured

        self._store = PgMetadataStore.from_config(section, clock=self.clock)
        return self._store

    def close(self) -> None:
        close = getattr(self._store, "close", None)
        if close is not None:
            with contextlib.suppress(MetadataError):
                close()
        self._store = None

    # --- planning -------------------------------------------------------------------------------

    def rules(self) -> RuleRegistry:
        patterns = self._section("rules").get(
            "license_error_patterns", list(DEFAULT_LICENSE_ERROR_PATTERNS)
        )
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ConfigError(
                f"[rules].license_error_patterns must be a list of strings, got {patterns!r}"
            )
        return RuleRegistry.from_entry_points(
            settings=RuleSettings(license_error_patterns=tuple(patterns))
        )

    def toolchains(self, flow_dir: Path) -> ToolchainResolver:
        path = flow_dir / TOOLCHAINS_FILE
        if not path.exists():
            return _MissingToolchains(path)
        return StaticToolchainResolver(path)

    def stat_settings(self) -> StatCacheSettings:
        return StatCacheSettings.from_section(self._section("stat_cache"))

    def statcache_path(self) -> Path:
        return self.stat_settings().path or self._statcache_default

    def open_statcache(self, warn: Callable[[str], None]) -> StatCache:
        """The persistent stat cache, or an in-memory one (every file rehashed) with a warning.

        R7: `[stat_cache].path`, else `/var/tmp/ebs-<uid>/statcache.sqlite` in a directory of
        mode 0700 owned by the user. Never a failed plan: the cache only saves rehashing.
        """
        settings = self.stat_settings()
        path = settings.path or self._statcache_default
        try:
            if settings.path is None:
                _private_dir(path.parent)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
            return StatCache(path, racy_window_s=settings.racy_window_s, clock=self.clock)
        except (OSError, SourceError, _UnsafeDir) as exc:
            warn(
                f"stat cache {path} is unusable ({exc}); every source file is rehashed this "
                "time. Set [stat_cache].path to a file on local disk to keep it"
            )
            return StatCache(
                Path(":memory:"), racy_window_s=settings.racy_window_s, clock=self.clock
            )

    def snapshotter(self, cas: FsCAS, statcache: StatCache, *, rehash: bool) -> SourceSnapshotter:
        settings = self.stat_settings()
        return SourceSnapshotter(
            cas,
            statcache,
            clock=self.clock,
            rehash=rehash,
            audit_fraction=settings.audit_fraction,
            untrusted_mounts=settings.untrusted_mounts,
        )

    # --- execution ------------------------------------------------------------------------------

    def runner_argv(self) -> Sequence[str]:
        beside = Path(sys.executable).parent / "ebs-runner"
        if beside.exists():
            return (str(beside),)
        found = shutil.which("ebs-runner", path=self.environ.get("PATH"))
        return (found or "ebs-runner",)

    @contextmanager
    def open_executor(self, name: str, *, domain: str, log_dir: Path) -> Iterator[Executor]:
        del domain  # the local executor passes it per batch
        if name != "local":
            raise ExecutorError(f"unknown executor {name!r}; available: {', '.join(EXECUTORS)}")
        executor = LocalExecutor(log_dir=log_dir, runner_argv=self.runner_argv(), env=self.environ)
        try:
            yield executor
        finally:
            executor.close()
