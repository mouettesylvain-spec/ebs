"""SLURM account, QOS and node constraint of a build's jobs (P1-03 R3, R4).

Team priority is SLURM's own business (fair-share, QOS); ebs only says which account and QOS a
job belongs to. The flow's `project` maps to an account through `[slurm.account_map]`; the
build kind picks the QOS from `[slurm.qos]` (`default`, `ci`, `release`). An empty account map
or QOS leaves the option off, so SLURM uses the user's default account and QOS.

The node OS is not in the action key; `[slurm].os_constraint` (a node feature such as `rhel9`)
pins jobs to nodes of the right OS image instead. A step may need more features
(`resources.constraint`), which are ANDed with it. None of this is in the action key (I3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

from ebs.core.errors import ConfigError, ExecutorError
from ebs.flow.model import CONSTRAINT_PATTERN, and_constraints

__all__ = [
    "BUILD_KINDS",
    "BuildKind",
    "JobPolicy",
    "QosSettings",
    "job_constraint",
    "parse_account_map",
    "parse_os_constraint",
    "parse_qos",
    "resolve_account",
    "select_qos",
]

BuildKind: TypeAlias = Literal["dev", "ci", "release"]
"""`ci`: `ebs build --ci`; `release`: a build made to be released; `dev`: everything else."""
BUILD_KINDS: Final[tuple[BuildKind, ...]] = ("dev", "ci", "release")

_NAME_RE: Final = re.compile(r"^[A-Za-z0-9_.+-]+$", re.ASCII)  # as SbatchOptions accepts
_CONSTRAINT_RE: Final = re.compile(CONSTRAINT_PATTERN, re.ASCII)
_QOS_KEYS: Final = ("default", "ci", "release")


@dataclass(frozen=True)
class QosSettings:
    """`[slurm.qos]`: the QOS per build kind; None leaves it to SLURM (ci/release: the default)."""

    default: str | None = None
    ci: str | None = None
    release: str | None = None


def resolve_account(account_map: Mapping[str, str], project: str | None) -> str | None:
    """The SLURM account of `project`; None when no map is configured.

    ConfigError if a map is configured but `project` is not in it, listing the known projects.
    """
    if not account_map:
        return None
    if project is None:
        raise ConfigError(
            "[slurm.account_map] is set but the SLURM executor was not told the flow's project; "
            "pass the plan's project to pick the account"
        )
    account = account_map.get(project)
    if account is None:
        raise ConfigError(
            f'project {project!r} has no SLURM account: add `{project} = "<account>"` to '
            f"[slurm.account_map] in the site config (known projects: "
            f"{', '.join(sorted(account_map))})"
        )
    return account


def select_qos(qos: QosSettings, kind: BuildKind) -> str | None:
    """`qos.ci` for CI builds, `qos.release` for release builds, else (or if unset) the default."""
    chosen = {"ci": qos.ci, "release": qos.release}.get(kind)
    return chosen if chosen is not None else qos.default


def job_constraint(os_constraint: str | None, step: Iterable[str]) -> str | None:
    """`--constraint`: the OS image feature ANDed with the step's features, without repeats."""
    exprs = [*([] if os_constraint is None else [os_constraint]), *step]
    for expr in exprs:
        if _CONSTRAINT_RE.fullmatch(expr) is None:
            raise ExecutorError(
                f"invalid node constraint {expr!r}: expected node features joined by '&' "
                "(e.g. 'rhel9&avx512')"
            )
    return and_constraints(*exprs)


@dataclass(frozen=True)
class JobPolicy:
    """What every job of one build gets on top of its resources: account, QOS, constraint."""

    account: str | None = None
    qos: str | None = None
    os_constraint: str | None = None

    @classmethod
    def resolve(
        cls,
        *,
        account_map: Mapping[str, str],
        qos: QosSettings,
        os_constraint: str | None,
        project: str | None,
        build_kind: BuildKind,
    ) -> JobPolicy:
        if build_kind not in BUILD_KINDS:
            raise ConfigError(
                f"unknown build kind {build_kind!r}; expected one of {', '.join(BUILD_KINDS)}"
            )
        return cls(
            account=resolve_account(account_map, project),
            qos=select_qos(qos, build_kind),
            os_constraint=os_constraint,
        )


# --- [slurm] config ----------------------------------------------------------------------------


def _name(value: object, where: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ConfigError(
            f"{where} must be a SLURM name (letters, digits, '_', '.', '+', '-'), got {value!r}"
        )
    return value


def parse_account_map(value: object) -> Mapping[str, str]:
    """`[slurm.account_map]`: project -> SLURM account."""
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ConfigError(
            f'[slurm.account_map] must be a table of project = "account", got {value!r}'
        )
    out: dict[str, str] = {}
    for project, account in value.items():
        if not isinstance(project, str):
            raise ConfigError(f"[slurm.account_map] key {project!r} is not a project name")
        out[project] = _name(account, f"[slurm.account_map].{project}")
    return MappingProxyType(dict(sorted(out.items())))


def parse_qos(value: object) -> QosSettings:
    """`[slurm.qos]`: `default`, `ci`, `release`; each optional (empty string = unset)."""
    if value is None:
        return QosSettings()
    if not isinstance(value, Mapping):
        raise ConfigError(
            f"[slurm.qos] must be a table with keys {', '.join(_QOS_KEYS)}, got {value!r}"
        )
    unknown = sorted(str(k) for k in set(value) - set(_QOS_KEYS))
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [slurm.qos]: {', '.join(unknown)}; expected {', '.join(_QOS_KEYS)}"
        )
    picked: dict[str, str | None] = {}
    for key in _QOS_KEYS:
        raw = value.get(key, "")
        picked[key] = None if raw == "" else _name(raw, f"[slurm.qos].{key}")
    return QosSettings(**picked)


def parse_os_constraint(value: object) -> str | None:
    """`[slurm].os_constraint`: node features (usually one, the OS image) joined by '&'."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or _CONSTRAINT_RE.fullmatch(value) is None:
        raise ConfigError(
            f"[slurm].os_constraint must be node features joined by '&' (e.g. 'rhel9'), "
            f"got {value!r}"
        )
    return value
