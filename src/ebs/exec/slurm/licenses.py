"""EDA license requests for SLURM jobs (P1-03 R1, R2; architecture.md § Licenses).

A step needs the license features of its toolchain plus its own; the step's count wins per
feature (`merge_licenses`). The executor asks SLURM for them with `sbatch --licenses`, so a job
starts only once its licenses are free. Licenses are SLURM *remote* licenses named
`<feature>@<server>` (`[slurm].license_server`, default `flexlm`); with
`[slurm].license_fuzzy = true` the request names the feature only and SLURM's
`LicenseParameters=RemoteFuzzyMatch` finds the server. A flow may pin a server for one feature
by writing `feature@server`; that request is passed as written.

Feature names are checked here (`FEATURE_PATTERN`), not against SLURM's list: SLURM rejects an
unknown feature at submission, and `license_error` turns that rejection into an error naming the
requested features. Licenses are never part of the action key (I3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from ebs.core.errors import ConfigError, ExecutorError
from ebs.flow.model import merge_licenses
from ebs.plan.types import ActionSpec

__all__ = [
    "DEFAULT_LICENSE_SERVER",
    "FEATURE_PATTERN",
    "batch_licenses",
    "check_license_server",
    "license_error",
    "license_request",
    "merge_licenses",
    "render_licenses",
]

FEATURE_PATTERN: Final = r"^[A-Za-z0-9_.-]+$"
DEFAULT_LICENSE_SERVER: Final = "flexlm"
_FEATURE_RE: Final = re.compile(FEATURE_PATTERN, re.ASCII)
# What SLURM says when it does not know a license (or cannot ever grant the count asked for).
_LICENSE_REJECTED: Final = re.compile(r"licen[cs]e", re.IGNORECASE)


def check_license_server(server: object) -> str:
    """`[slurm].license_server` must be a non-empty name of the FEATURE_PATTERN alphabet."""
    if not isinstance(server, str) or not _FEATURE_RE.match(server):
        raise ConfigError(
            f"[slurm].license_server must be a license server name like 'flexlm' "
            f"(letters, digits, '_', '.', '-'), got {server!r}"
        )
    return server


def _check(name: object, count: object) -> tuple[str, str | None]:
    if not isinstance(name, str):
        raise ExecutorError(f"license feature {name!r} is not a string")
    feature, at, server = name.partition("@")
    if not _FEATURE_RE.match(feature) or (at and not _FEATURE_RE.match(server)):
        raise ExecutorError(
            f"invalid license feature {name!r}: expected 'feature' or 'feature@server' made of "
            "letters, digits, '_', '.' and '-'; fix the licenses of the step or its toolchain"
        )
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ExecutorError(f"license {name!r}: count must be a positive integer, got {count!r}")
    return feature, server if at else None


def license_request(licenses: Mapping[str, int], *, server: str, fuzzy: bool) -> dict[str, int]:
    """The `--licenses` entries for `licenses`: `feat@server` (or `feat` when fuzzy) -> count.

    Raises ExecutorError for a malformed feature or count, ConfigError for a bad server.
    """
    check_license_server(server)
    request: dict[str, int] = {}
    for name, count in licenses.items():
        feature, pinned = _check(name, count)
        if pinned is not None:
            key = f"{feature}@{pinned}"
        else:
            key = feature if fuzzy else f"{feature}@{server}"
        if key in request:
            raise ExecutorError(f"license {key!r} is requested twice: {sorted(licenses)}")
        request[key] = count
    return dict(sorted(request.items()))


def render_licenses(request: Mapping[str, int]) -> str:
    """The `-L` value: `name:count` entries, sorted, comma-separated ("" for none)."""
    return ",".join(f"{name}:{count}" for name, count in sorted(request.items()))


def batch_licenses(actions: Iterable[ActionSpec]) -> dict[str, int]:
    """One request for every action of a job (array): the largest count per license."""
    out: dict[str, int] = {}
    for action in actions:
        for name, count in action.licenses.items():
            out[name] = max(out.get(name, 0), count)
    return dict(sorted(out.items()))


def license_error(
    exc: ExecutorError, request: Mapping[str, int], *, step: str
) -> ExecutorError | None:
    """An error naming the requested licenses if SLURM rejected the job for them, else None."""
    if not request or not _LICENSE_REJECTED.search(str(exc)):
        return None
    return ExecutorError(
        f"SLURM rejected the license request of step {step!r} "
        f"(--licenses={render_licenses(request)}): {exc}; check that each of "
        f"{', '.join(sorted(request))} exists with enough tokens (`scontrol show licenses`), "
        "or fix the licenses of the step or its toolchain"
    )
