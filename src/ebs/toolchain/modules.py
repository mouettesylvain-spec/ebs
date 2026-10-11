"""Module snapshots: `module purge; module load MODULE` captured in a clean shell (P1-07).

Supports Environment Modules 4/5 (`$MODULESHOME/libexec/modulecmd.tcl`) and Lmod
(`$LMOD_CMD` or `$MODULESHOME/libexec/lmod`). The module command runs inside `capture_env`'s clean
bash through the fixed helper `module_load.bash`, so the module name is never parsed as shell code.

A snapshot holds the *env diff*: the variables that purge + load added or changed relative to
the same clean shell without them (variables a module unsets cannot be represented and are
dropped). Install roots are the directories the changed variables point into, plus explicit
extra roots (UVM, VIP trees). The env never depends on who registers: the clean shell has a fresh
HOME (normalized to `$HOME` by `capture_env`), the registering user's real HOME is normalized the
same way, and a value naming the registering user is refused.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from ebs.core.errors import ToolchainError
from ebs.core.log import get_logger
from ebs.runner.config import DEFAULT_PASSTHROUGH_ENV
from ebs.toolchain.env import (
    DEFAULT_LANG,
    DEFAULT_PATH,
    HOME_PLACEHOLDER,
    EnvRunner,
    capture_env,
    run_subprocess,
)

__all__ = [
    "Flavour",
    "ModuleSnapshot",
    "ModuleSystem",
    "derive_install_roots",
    "detect_module_system",
    "snapshot_module",
]

Flavour = Literal["envmodules", "lmod"]

MODULE_INTERNAL_VARS: Final = frozenset(
    {"MODULEPATH", "MODULESHOME", "LOADEDMODULES", "_LMFILES_", "MODULEPATH_ROOT"}
)
MODULE_INTERNAL_PREFIXES: Final = ("__LMOD", "_ModuleTable", "LMOD_", "__MODULES_", "MODULES_")
"""Bookkeeping variables of the module systems: part of the env, never a source of roots."""

SYSTEM_DIRS: Final = (
    *("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc"),
    *("/proc", "/sys", "/dev", "/run", "/boot"),
)
TOP_LEVEL_DIRS: Final = frozenset({"/", "/tmp", "/var", "/var/tmp", "/opt", "/home"})
LEAF_DIRS: Final = frozenset(
    {"bin", "sbin", "lib", "lib32", "lib64", "libexec", "include", "share", "man", "etc"}
)
"""A root ending in one of these is widened to its parent (`<prefix>/bin` -> `<prefix>`)."""

_SEGMENT: Final = re.compile(r"[A-Za-z0-9_+][A-Za-z0-9_.+-]*")
_ENVMODULES_VERSION: Final = re.compile(r"Modules Release (\S+)")
_LMOD_VERSION: Final = re.compile(r"Modules based on Lua: Version (\S+)")
_HELPER: Final = "module_load.bash"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModuleSystem:
    """The module system found on this host. Not part of any toolchain id."""

    flavour: Flavour
    version: str
    command: Path  # prints shell code: `<command> bash load <module>`
    home: Path  # MODULESHOME


@dataclass(frozen=True, slots=True)
class ModuleSnapshot:
    module: str
    version: str
    env: Mapping[str, str] = field(hash=False)  # the env diff, read-only and sorted
    install_roots: tuple[Path, ...]
    system: ModuleSystem
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(sorted(self.env.items()))))


def detect_module_system(
    environ: Mapping[str, str], *, runner: EnvRunner = run_subprocess, timeout_s: float = 30.0
) -> ModuleSystem:
    """Find the module command from `$LMOD_CMD` / `$MODULESHOME` and check its `--version`."""
    flavour: Flavour
    lmod_cmd = environ.get("LMOD_CMD")
    home_value = environ.get("MODULESHOME")
    if lmod_cmd and Path(lmod_cmd).is_file():
        flavour, command = "lmod", Path(lmod_cmd)
        home = Path(home_value) if home_value else command.parent.parent
    elif home_value:
        home = Path(home_value)
        if (home / "libexec" / "lmod").is_file():
            flavour, command = "lmod", home / "libexec" / "lmod"
        elif (home / "libexec" / "modulecmd.tcl").is_file():
            flavour, command = "envmodules", home / "libexec" / "modulecmd.tcl"
        else:
            raise ToolchainError(
                f"MODULESHOME={home} has neither libexec/lmod (Lmod) nor libexec/modulecmd.tcl "
                "(Environment Modules 4/5); run `module --version` to check the module setup"
            )
    else:
        raise ToolchainError(
            "no module system found: MODULESHOME is not set; initialize Environment Modules or "
            "Lmod in your shell (so that `module --version` works) and retry"
        )
    version = _version(flavour, command, runner, timeout_s)
    _log.debug("module system", flavour=flavour, version=version, command=str(command))
    return ModuleSystem(flavour, version, command, home)


def _version(flavour: Flavour, command: Path, runner: EnvRunner, timeout_s: float) -> str:
    env = {"PATH": DEFAULT_PATH, "LANG": DEFAULT_LANG}
    try:
        proc = runner([str(command), "bash", "--version"], env, "/", timeout_s)
    except OSError as exc:
        raise ToolchainError(f"cannot run module command {command}: {exc.strerror}") from exc
    except TimeoutError as exc:  # pragma: no cover  # subprocess.TimeoutExpired is not one
        raise ToolchainError(f"{command} --version timed out") from exc
    text = (proc.stdout + proc.stderr).decode("utf-8", "replace")
    pattern = _LMOD_VERSION if flavour == "lmod" else _ENVMODULES_VERSION
    match = pattern.search(text)
    if match is None:
        raise ToolchainError(
            f"cannot read the {flavour} version from `{command} bash --version` "
            f"(output: {text.strip()[:200]!r}); is this really "
            f"{'Lmod' if flavour == 'lmod' else 'Environment Modules'}?"
        )
    version = match.group(1)
    if flavour == "envmodules":
        major = re.match(r"\d+", version)
        if major is None or int(major.group()) < 4:
            raise ToolchainError(
                f"Environment Modules {version} at {command} is not supported: ebs needs "
                "Environment Modules 4 or later, or Lmod"
            )
    return version


def check_module_name(module: str) -> str:
    """`module` if it names an explicit version (`name/version`), else ToolchainError."""
    parts = module.split("/")
    if len(parts) < 2 or not all(_SEGMENT.fullmatch(p) for p in parts):
        raise ToolchainError(
            f"invalid module {module!r}: register an explicit version as <name>/<version> "
            "(e.g. questa/2025.2), using letters, digits and '_.+-'; a default version could "
            "change under the same name"
        )
    return module


def snapshot_module(
    module: str,
    *,
    system: ModuleSystem,
    environ: Mapping[str, str],
    user: str,
    extra_roots: Sequence[Path] = (),
    timeout_s: float = 120.0,
    runner: EnvRunner = run_subprocess,
    site_env: Sequence[str] = DEFAULT_PASSTHROUGH_ENV,
) -> ModuleSnapshot:
    """Capture `module purge; module load MODULE` and derive the install roots (R1, R5, R6).

    `environ` is the caller's environment: only MODULEPATH (required) is taken from it, plus
    HOME to normalize; `user` is the registering user, whose name must not appear in the env
    outside the install roots. Variables matching `site_env` (license servers: the runner passes
    the caller's through, never part of a key) are dropped from the snapshot.
    """
    check_module_name(module)
    for root in extra_roots:
        if not root.is_absolute():
            raise ToolchainError(
                f"install root {str(root)!r} must be an absolute path; pass --root /abs/path"
            )
    modulepath = environ.get("MODULEPATH")
    if not modulepath:
        raise ToolchainError(
            "MODULEPATH is not set: ebs loads the module with your MODULEPATH; run "
            "`module use <dir>` (or initialize the module system) and retry"
        )
    base = {"MODULESHOME": str(system.home), "MODULEPATH": modulepath}
    baseline = capture_env([], base_env=base, timeout_s=timeout_s, runner=runner)
    with resources.as_file(resources.files("ebs.toolchain") / _HELPER) as helper:
        loaded = capture_env(
            ["source", str(helper), str(system.command), module],
            base_env=base,
            timeout_s=timeout_s,
            runner=runner,
        )
    dropped = sorted(set(baseline) - set(loaded))
    if dropped:
        _log.debug("module unset variables; not recorded", module=module, names=dropped)
    env = {k: v for k, v in loaded.items() if baseline.get(k) != v}
    site = sorted(k for k in env if any(fnmatch.fnmatchcase(k, p) for p in site_env))
    if site:
        _log.debug("site variables set by module; not recorded", module=module, names=site)
    env = _normalize_home({k: v for k, v in env.items() if k not in site}, home=environ.get("HOME"))
    if module not in env.get("LOADEDMODULES", "").split(":"):
        raise ToolchainError(
            f"`module load {module}` succeeded but {module} is not in LOADEDMODULES "
            f"({env.get('LOADEDMODULES', '')!r}); check the modulefile"
        )
    roots = derive_install_roots(env, modulepath=modulepath.split(":"), extra=extra_roots)
    if not roots:
        raise ToolchainError(
            f"module {module} sets no variable pointing at an install directory; name the "
            "tool's installation directories with --root /path (repeatable)"
        )
    warnings = _check_user(env, module=module, user=user, roots=roots)
    for warning in warnings:
        _log.warning("toolchain env names the registering user", detail=warning)
    return ModuleSnapshot(
        module=module,
        version=module.rpartition("/")[2],
        env=env,
        install_roots=roots,
        system=system,
        warnings=warnings,
    )


def _normalize_home(env: Mapping[str, str], *, home: str | None) -> dict[str, str]:
    """Replace the registering user's real HOME (at a path boundary) with `$HOME`."""
    homes = {home, os.path.realpath(home)} if home and home != "/" else set()
    patterns = [
        re.compile(r"(?<![^:])" + re.escape(h.rstrip("/")) + r"(?![^/:])")
        for h in sorted(homes, key=len, reverse=True)
    ]
    out: dict[str, str] = {}
    for name, value in env.items():
        for pattern in patterns:
            value = pattern.sub(lambda _: HOME_PLACEHOLDER, value)
        out[name] = value
    return out


def _check_user(
    env: Mapping[str, str], *, module: str, user: str, roots: Sequence[Path]
) -> tuple[str, ...]:
    """Refuse a value naming the registering user (as a whole word) outside the install roots.

    Inside an install root the name is part of where the tool is installed (a service account
    `eda` owning `/tools/eda/...`), which is the same for every user: only a warning.
    """
    if not user:
        return ()
    user_re = re.compile(rf"(?<![A-Za-z0-9._-]){re.escape(user)}(?![A-Za-z0-9._-])")
    warnings: list[str] = []
    for name, value in env.items():
        for part in value.split(":"):
            if not user_re.search(part):
                continue
            path = Path(os.path.normpath(part)) if part.startswith("/") else None
            if path is not None and any(path.is_relative_to(r) for r in roots):
                warnings.append(
                    f"{name} names user {user!r} inside install root {part!r}; fine if that is "
                    "where the tool is installed for everyone"
                )
                continue
            raise ToolchainError(
                f"module {module} sets {name}={value!r}, which names the registering user "
                f"{user!r}; a toolchain must not depend on who registers it. Fix the modulefile "
                "or register as a service account"
            )
    return tuple(warnings)


def derive_install_roots(
    env: Mapping[str, str], *, modulepath: Sequence[str] = (), extra: Sequence[Path] = ()
) -> tuple[Path, ...]:
    """Existing directories the env points into, widened and de-nested, plus `extra`, sorted.

    Every absolute entry of a changed variable (split on `:`) counts, except module bookkeeping
    variables, system directories and the modulefile directories. A file counts by its
    directory, and a `bin`/`lib`-style directory by its parent.
    """
    module_dirs = [Path(os.path.normpath(p)) for p in modulepath if p.startswith("/")]
    candidates: set[Path] = {Path(os.path.normpath(r)) for r in extra}
    for name, value in env.items():
        if name in MODULE_INTERNAL_VARS or name.startswith(MODULE_INTERNAL_PREFIXES):
            continue
        for part in value.split(":"):
            root = _root_of(part)
            if root is None or any(root.is_relative_to(d) for d in module_dirs):
                continue
            candidates.add(root)
    kept: list[Path] = []
    for path in sorted(candidates):
        if not any(path.is_relative_to(k) for k in kept):
            kept.append(path)
    return tuple(kept)


def _root_of(part: str) -> Path | None:
    if not part.startswith("/"):
        return None
    path = Path(os.path.normpath(part))
    if path.is_file():
        path = path.parent
    elif not path.is_dir():
        return None
    if path.name in LEAF_DIRS:
        path = path.parent
    text = str(path)
    if text in TOP_LEVEL_DIRS or any(path.is_relative_to(d) for d in SYSTEM_DIRS):
        return None
    return path
