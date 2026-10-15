"""Sweep planning (P1-09 R4, I15): what to delete, in which order, given one snapshot.

Order (I15): action-cache rows first, blobs later. A run

1. deletes cache rows that marking did not keep and that were unused for more than `ttl_days`,
2. tombstones every object nothing keeps any more: not reachable from a root, a surviving cache
   entry or an object used within `ttl_days` (`deleted_rows_at = now`, delete after `grace`),
3. drops tombstones of objects that are kept again (reached, or touched),
4. deletes the bytes of objects tombstoned by an earlier run whose grace period is over, that are
   still unreached and whose blob row was not touched since the tombstone, parents before
   children, never a child of a tree that stays.

"Used" is the effective last access: an object's own `blobs.last_access`, or the most recent
access of a tree containing it (children are rarely touched one by one). A CAS object without a
blob row and without a parent (stored before access tracking, or by a crashed writer) is adopted:
registered as used now, so it gets a full TTL.

Planning is pure; `ebs.gc.run` loads the snapshot and applies the plan.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from ebs.core.digest import Digest
from ebs.core.errors import ConfigError
from ebs.gc.mark import Marked, mark
from ebs.gc.quota import QuotaPlan, plan_quota
from ebs.gc.roots import TreeGraph, root_set
from ebs.meta.api import (
    TOUCH_INTERVAL_S,
    DomainPolicy,
    GcBlob,
    GcCacheEntry,
    GcRoots,
    Tombstone,
)

__all__ = ["GcConfig", "Snapshot", "SweepPlan", "effective_access", "entry_refs", "plan_sweep"]

_CONFIG_KEYS = frozenset({"grace_hours", "tmp_max_age_hours"})


@dataclass(frozen=True, slots=True)
class GcConfig:
    """The `[gc]` section of ebs.toml; per-domain TTL and quota live in the `domains` table."""

    grace_hours: int = 24
    tmp_max_age_hours: int = 24

    def __post_init__(self) -> None:
        for name in sorted(_CONFIG_KEYS):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ConfigError(
                    f"[gc].{name} must be a whole number of hours >= 1, got {value!r}"
                )
        if self.grace_hours * 3600 < TOUCH_INTERVAL_S:  # pragma: no cover - 1 h minimum above
            raise ConfigError("[gc].grace_hours must be at least the touch interval (1 h)")

    @property
    def grace(self) -> timedelta:
        return timedelta(hours=self.grace_hours)

    @property
    def tmp_max_age(self) -> timedelta:
        return timedelta(hours=self.tmp_max_age_hours)

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> GcConfig:
        unknown = sorted(set(section) - _CONFIG_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [gc]: {', '.join(unknown)}; expected "
                f"{', '.join(sorted(_CONFIG_KEYS))}"
            )
        return cls(**section)  # type: ignore[arg-type]  # validated in __post_init__


@dataclass(frozen=True)
class Snapshot:
    """What one GC run sees of a domain: the CAS (source of truth for bytes) and the database."""

    domain: str
    now: datetime
    policy: DomainPolicy
    sizes: Mapping[Digest, int]  # every CAS object with its size on disk
    trees: frozenset[Digest]  # the objects that are tree manifests
    graph: TreeGraph
    blobs: Mapping[Digest, GcBlob]
    entries: tuple[GcCacheEntry, ...]
    roots: GcRoots
    tombstones: Mapping[Digest, Tombstone]


@dataclass(frozen=True)
class SweepPlan:
    marked: Marked
    keep: frozenset[Digest]  # everything that stays (closed over trees)
    access: Mapping[Digest, datetime]  # effective last use of every CAS object
    adopt: tuple[Digest, ...]  # CAS objects to register (no row, no parent)
    delete_rows: tuple[Digest, ...]  # stale cache entries (TTL)
    tombstone: tuple[Digest, ...]  # newly unkept objects (includes quota evictions)
    resurrect: tuple[Digest, ...]  # tombstones to drop
    due: tuple[Digest, ...]  # objects whose bytes go now, parents first
    quota: QuotaPlan | None


def entry_refs(entry: GcCacheEntry) -> list[Digest]:
    return [*entry.outputs, *([entry.log] if entry.log is not None else [])]


def effective_access(snap: Snapshot, adopted: frozenset[Digest]) -> dict[Digest, datetime]:
    """Own last access, or the latest of any tree containing the object; adopted objects: now."""
    depth = snap.graph.depths()
    parents = snap.graph.parents()
    access: dict[Digest, datetime] = {}
    floor = datetime.min.replace(tzinfo=snap.now.tzinfo)
    for d in sorted(set(snap.sizes) | set(depth), key=lambda x: (depth.get(x, 0), str(x))):
        own = snap.blobs[d].last_access if d in snap.blobs else floor
        if d in adopted:
            own = snap.now
        inherited = (access[p] for p in parents.get(d, ()) if p in access)
        access[d] = max(own, *inherited) if d in parents else own
    return {d: access[d] for d in snap.sizes}


def plan_sweep(snap: Snapshot, config: GcConfig) -> SweepPlan:
    now, graph = snap.now, snap.graph
    cutoff = now - timedelta(days=snap.policy.ttl_days)
    parents = graph.parents()
    adopt = tuple(
        sorted((d for d in snap.sizes if d not in snap.blobs and d not in parents), key=str)
    )
    access = effective_access(snap, frozenset(adopt))
    marked = mark(root_set(snap.roots), snap.entries, graph)

    stale = {e.key for e in snap.entries if e.key not in marked.entries and e.last_access <= cutoff}
    surviving = [e for e in snap.entries if e.key not in stale]
    # Used within the TTL; a tombstone nobody touched since does not count as a use (a quota
    # eviction is often of an object used within the TTL, and must stay evicted).
    recent = {d for d, at in access.items() if at > cutoff and not _untouched_tombstone(snap, d)}

    def keep_set(rows: list[GcCacheEntry], fresh: set[Digest]) -> frozenset[Digest]:
        seeds = set(marked.reachable) | fresh
        for e in rows:
            seeds.update(entry_refs(e))
        return frozenset(graph.closure(seeds))

    keep = keep_set(surviving, recent)
    unkept = set(snap.sizes) - keep
    quota = plan_quota(
        snap.policy,
        snap.sizes,
        protected=marked.reachable,
        pending=unkept | (set(snap.tombstones) - keep),
        access=access,
        not_after=now - config.grace,
        entries=surviving,
        graph=graph,
    )
    if quota is not None and quota.evicted:
        evicted = set(quota.evicted)
        quota_rows = set(quota.rows)
        keep = keep_set([e for e in surviving if e.key not in quota_rows], recent - evicted)

    tombstone = tuple(
        sorted((d for d in snap.sizes if d not in keep and d not in snap.tombstones), key=str)
    )
    # Dropped: kept again, gone from the CAS, or touched since the tombstone (if still unkept,
    # the next run tombstones it afresh, so a use always restarts the grace period).
    resurrect = tuple(
        sorted(
            (
                d
                for d in snap.tombstones
                if d in keep or d not in snap.sizes or not _untouched_tombstone(snap, d)
            ),
            key=str,
        )
    )
    due_set = {
        d
        for d, stone in snap.tombstones.items()
        if d in snap.sizes
        and d not in keep
        and stone.blob_delete_after <= now
        and _untouched_tombstone(snap, d)
    }
    # A tree that stays keeps all it contains, even children that are due on their own.
    staying_trees = [t for t in snap.trees if t not in due_set]
    due_set -= graph.closure(staying_trees)
    depth = graph.depths()
    due = tuple(sorted(due_set, key=lambda d: (depth.get(d, 0), str(d))))
    return SweepPlan(
        marked=marked,
        keep=keep,
        access=access,
        adopt=adopt,
        delete_rows=tuple(sorted(stale, key=str)),
        tombstone=tombstone,
        resurrect=resurrect,
        due=due,
        quota=quota,
    )


def _untouched_tombstone(snap: Snapshot, d: Digest) -> bool:
    """`d` is tombstoned and its blob row was not touched since."""
    stone = snap.tombstones.get(d)
    if stone is None:
        return False
    blob = snap.blobs.get(d)
    return (
        blob is None or blob.last_access < stone.deleted_rows_at
    )  # a touch at that instant counts
