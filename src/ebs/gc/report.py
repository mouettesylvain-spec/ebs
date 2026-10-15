"""The GC report (P1-09 R6): what a run did, or with `--dry-run` would do.

A dry run computes the same plan from the same snapshot and reports it without changing
anything, so its report equals the real run's (except `dry_run`) when nothing else changed in
between. Reclaimable bytes are those of every object the run does not keep (deleted now,
tombstoned, or evicted), broken down per step, project and build that recorded it as a plan,
output or log; an object counts for each of them, so the breakdowns may add up to more than the
total, and objects nobody recorded (sources, plans) count only in the total.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ebs.core.digest import Digest
from ebs.gc.roots import TreeGraph
from ebs.meta.api import GcAttribution

__all__ = ["GcReport", "QuotaReport", "Reclaimable", "reclaimable", "render"]

_FROZEN = ConfigDict(frozen=True, extra="forbid", strict=True)


class Reclaimable(BaseModel):
    model_config = _FROZEN

    objects: int
    bytes: int
    by_step: dict[str, int]
    by_project: dict[str, int]
    by_build: dict[str, int]  # build id -> bytes


class QuotaReport(BaseModel):
    model_config = _FROZEN

    quota_bytes: int
    usage_bytes: int
    pending_bytes: int
    high_water_bytes: int
    low_water_bytes: int
    evicted: list[str]  # digests, least recently used first
    evicted_bytes: int


class GcReport(BaseModel):
    """One run of `ebs gc` for one domain; stored in `gc_runs.report` (canonical JSON)."""

    model_config = _FROZEN

    v: Literal[1] = 1
    domain: str
    dry_run: bool
    started_at: str  # ISO 8601, UTC
    objects: int  # in the CAS
    bytes: int
    marked: int  # reachable from a root (I15)
    kept: int
    adopted: int  # CAS objects without a blob row, registered now
    cache_rows_deleted: int
    tombstoned: int
    tombstoned_bytes: int
    resurrected: int
    deleted: int
    deleted_bytes: int
    tmp_removed: int
    quota: QuotaReport | None
    reclaimable: Reclaimable


def reclaimable(
    sizes: Mapping[Digest, int],
    unkept: Iterable[Digest],
    attribution: Iterable[GcAttribution],
    graph: TreeGraph,
) -> Reclaimable:
    """Bytes of `unkept`, in total and per step, project and build (trees count for all they
    contain)."""
    gone = {d for d in unkept if d in sizes}
    owners: dict[Digest, set[tuple[str, str, str]]] = {}
    for a in attribution:
        owners.setdefault(a.digest, set()).add((a.step, a.project, str(int(a.build))))
    found: dict[Digest, set[tuple[str, str, str]]] = {}
    for digest, who in owners.items():
        for d in graph.closure([digest]) & gone:
            found.setdefault(d, set()).update(who)
    by: tuple[dict[str, int], dict[str, int], dict[str, int]] = ({}, {}, {})
    for d, who in found.items():
        for i, table in enumerate(by):
            for key in {w[i] for w in who}:
                table[key] = table.get(key, 0) + sizes[d]
    return Reclaimable(
        objects=len(gone),
        bytes=sum(sizes[d] for d in gone),
        by_step=dict(sorted(by[0].items())),
        by_project=dict(sorted(by[1].items())),
        by_build=dict(sorted(by[2].items(), key=lambda kv: int(kv[0]))),
    )


def _size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{n} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")  # pragma: no cover


def render(report: GcReport) -> list[str]:
    """The human-readable report, one line per item."""
    verb = "would " if report.dry_run else ""
    lines = [
        f"gc {report.domain}{' (dry run)' if report.dry_run else ''} at {report.started_at}",
        f"  CAS: {report.objects} objects, {_size(report.bytes)}; marked {report.marked}, "
        f"kept {report.kept}, adopted {report.adopted}",
        f"  cache rows {verb}deleted: {report.cache_rows_deleted}",
        f"  {verb}tombstoned: {report.tombstoned} ({_size(report.tombstoned_bytes)}); "
        f"resurrected: {report.resurrected}",
        f"  {verb}deleted: {report.deleted} ({_size(report.deleted_bytes)}); "
        f"stale tmp files {verb}removed: {report.tmp_removed}",
    ]
    q = report.quota
    if q is not None:
        lines.append(
            f"  quota {_size(q.quota_bytes)}: usage {_size(q.usage_bytes)} "
            f"({_size(q.pending_bytes)} pending), high {_size(q.high_water_bytes)}, low "
            f"{_size(q.low_water_bytes)}; {verb}evicted {len(q.evicted)} "
            f"({_size(q.evicted_bytes)})"
        )
    r = report.reclaimable
    lines.append(f"  reclaimable: {r.objects} objects, {_size(r.bytes)}")
    for title, table in (("step", r.by_step), ("project", r.by_project), ("build", r.by_build)):
        for key, value in sorted(table.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {title} {key}: {_size(value)}")
    return lines
