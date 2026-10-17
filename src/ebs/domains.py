"""Confidentiality domains (NDA scopes) from config (P2-01, decision D7).

    [[domains]]
    name = "cpu-nda"
    unix_group = "cpu-nda"        # members may see the domain; owns its CAS and debug dirs
    cas_root = "/cas"             # objects under <cas_root>/<name>/ (default: [cas].root)
    debug_root = "/debug"         # logs under <debug_root>/<name>/ (default: [debug].root)
    ttl_days = 30                 # GC TTL (reported by `ebs domain list`; GC reads the DB row)
    quota = "50T"                 # optional, binary units

Each domain is a separate CAS root, debug area and cache namespace owned by its Unix group
(created by deploy/admin/create_domain.sh). Without any `[[domains]]` the site runs unrestricted
(development); with them, the CLI refuses flows of a domain the user is not in, and the metadata
service serves only the configured domains listed in `[meta_service].domains`.
"""

from __future__ import annotations

import grp
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.core.errors import ConfigError

__all__ = [
    "CAS_DIR_MODE",
    "CAS_SUBDIRS",
    "DOMAIN_NAME_PATTERN",
    "WRITABLE_DIR_MODE",
    "DirCheck",
    "DomainCheck",
    "DomainConfig",
    "Domains",
    "check_domain",
    "current_groups",
    "group_id",
]

DOMAIN_NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
"""The same rule as `ebs.meta.api.DOMAIN_PATTERN` and the fs CAS directory names."""

_KEYS: Final = frozenset({"name", "unix_group", "cas_root", "debug_root", "ttl_days", "quota"})
_UNITS: Final = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40, "P": 1 << 50}
_SIZE: Final = re.compile(r"^([1-9][0-9]*)([KMGTP])(?:i?B)?$")


@dataclass(frozen=True, slots=True)
class DomainConfig:
    name: str
    unix_group: str
    cas_root: Path | None = None  # None: [cas].root
    debug_root: Path | None = None  # None: [debug].root
    ttl_days: int = 30
    quota_bytes: int | None = None


def _path(value: object, where: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ConfigError(f"{where} must be an absolute path, got {value!r}")
    return Path(value)


def _quota(value: object, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    m = _SIZE.fullmatch(value) if isinstance(value, str) else None
    if m is None:
        raise ConfigError(f"{where} must be a size like 50T or 512G, got {value!r}")
    return int(m[1]) * _UNITS[m[2]]


def _domain(name: str, table: object) -> DomainConfig:
    where = f"[[domains]] {name!r}"
    if not isinstance(table, Mapping):
        raise ConfigError(f"{where} must be a table")
    unknown = set(table) - _KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed: {sorted(_KEYS)}")
    if re.fullmatch(DOMAIN_NAME_PATTERN, name) is None:
        raise ConfigError(
            f"{where}: invalid domain name; use letters, digits, '.', '_' or '-' (at most 128)"
        )
    group = table.get("unix_group")
    if not isinstance(group, str) or not group:
        raise ConfigError(f"{where}: unix_group is required (the group owning the domain)")
    ttl = table.get("ttl_days", 30)
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
        raise ConfigError(f"{where}: ttl_days must be a positive integer, got {ttl!r}")
    return DomainConfig(
        name=name,
        unix_group=group,
        cas_root=_path(table.get("cas_root"), f"{where}: cas_root"),
        debug_root=_path(table.get("debug_root"), f"{where}: debug_root"),
        ttl_days=ttl,
        quota_bytes=_quota(table.get("quota"), f"{where}: quota"),
    )


@dataclass(frozen=True)
class Domains:
    """The configured domains, by name; empty when the site configures none."""

    by_name: Mapping[str, DomainConfig]

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, object]]) -> Domains:
        """From the merged config (`load_config` keys `[[domains]]` by name)."""
        section = config.get("domains", {})
        found = {name: _domain(name, table) for name, table in sorted(section.items())}
        return cls(found)

    @property
    def configured(self) -> bool:
        return bool(self.by_name)

    def get(self, name: str) -> DomainConfig | None:
        return self.by_name.get(name)

    def require(self, name: str) -> DomainConfig:
        found = self.by_name.get(name)
        if found is None:
            known = ", ".join(sorted(self.by_name)) or "none"
            raise ConfigError(f"domain {name!r} is not configured in [[domains]] (known: {known})")
        return found

    def cas_root(self, name: str, default: Path) -> Path:
        """The root holding `<name>/` of the domain's CAS."""
        found = self.by_name.get(name)
        return default if found is None or found.cas_root is None else found.cas_root

    def debug_root(self, name: str, default: Path | None) -> Path | None:
        found = self.by_name.get(name)
        return default if found is None or found.debug_root is None else found.debug_root

    def is_member(self, name: str, groups: Iterable[str]) -> bool:
        """Whether a user in `groups` may see domain `name`; unconfigured sites allow all."""
        if not self.by_name:
            return True
        found = self.by_name.get(name)
        return found is not None and found.unix_group in set(groups)


def current_groups(
    *,
    getgroups: Callable[[], list[int]] = os.getgroups,
    getgid: Callable[[], int] = os.getgid,
    name_of: Callable[[int], str] | None = None,
) -> frozenset[str]:
    """The names of the calling process's groups (primary and supplementary)."""

    def lookup(gid: int) -> str | None:
        try:
            return name_of(gid) if name_of is not None else grp.getgrgid(gid).gr_name
        except KeyError:
            return None

    names = (lookup(g) for g in {getgid(), *getgroups()})
    return frozenset(n for n in names if n is not None)


@dataclass(frozen=True, slots=True)
class DirCheck:
    """One directory of a domain as `ebs domain check` found it."""

    path: Path
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DomainCheck:
    domain: str
    dirs: tuple[DirCheck, ...]
    free_bytes: int | None
    total_bytes: int | None
    problems: tuple[str, ...]  # domain-level: free space

    @property
    def ok(self) -> bool:
        return not self.problems and all(not d.problems for d in self.dirs)


# The layout deploy/admin/create_domain.sh makes (P2-01 R4): mode with setgid, per directory.
CAS_DIR_MODE: Final = 0o2750
CAS_SUBDIRS: Final = ("blobs", "trees", "tmp")
WRITABLE_DIR_MODE: Final = 0o2770


def _check_dir(path: Path, mode: int, gid: int | None, group: str) -> DirCheck:
    try:
        st = path.stat()
    except FileNotFoundError:
        return DirCheck(path, ("missing (run deploy/admin/create_domain.sh)",))
    except OSError as exc:
        return DirCheck(path, (f"cannot stat: {exc.strerror or exc}",))
    problems = []
    if not stat.S_ISDIR(st.st_mode):
        return DirCheck(path, ("not a directory",))
    got = stat.S_IMODE(st.st_mode)
    if got != mode:
        problems.append(f"mode {got:04o}, expected {mode:04o}")
    if gid is None:
        problems.append(f"group {group!r} does not exist on this host")
    elif st.st_gid != gid:
        problems.append(f"group id {st.st_gid}, expected {group!r} ({gid})")
    return DirCheck(path, tuple(problems))


def check_domain(
    domain: DomainConfig,
    *,
    cas_root: Path,
    debug_root: Path | None,
    group_id: Callable[[str], int | None],
    disk_usage: Callable[[Path], tuple[int, int]] | None = None,
    min_free_ratio: float = 0.10,
) -> DomainCheck:
    """Modes, group and free space of a domain's directories (`ebs domain check`)."""
    gid = group_id(domain.unix_group)
    base = cas_root / domain.name
    dirs = [_check_dir(base, CAS_DIR_MODE, gid, domain.unix_group)]
    dirs += [_check_dir(base / s, WRITABLE_DIR_MODE, gid, domain.unix_group) for s in CAS_SUBDIRS]
    if debug_root is not None:
        dirs.append(_check_dir(debug_root / domain.name, WRITABLE_DIR_MODE, gid, domain.unix_group))
    usage = disk_usage or _disk_usage
    free = total = None
    problems = []
    try:
        free, total = usage(base if base.exists() else cas_root)
    except OSError as exc:
        problems.append(f"cannot read free space: {exc.strerror or exc}")
    if free is not None and total and free < total * min_free_ratio:
        problems.append(
            f"only {free * 100 // total}% free on the CAS filesystem (below {min_free_ratio:.0%})"
        )
    return DomainCheck(domain.name, tuple(dirs), free, total, tuple(problems))


def _disk_usage(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return usage.free, usage.total


def group_id(name: str) -> int | None:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None
