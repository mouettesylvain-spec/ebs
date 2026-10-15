"""Quota pressure (P1-09 R5): above `high_water` * quota, evict unrooted objects, least recently
used first, until usage is at most `low_water` * quota.

Eviction follows I15 like the TTL sweep: the cache rows that reference an evicted object are
deleted now and the object is tombstoned; its bytes go after the grace period. Bytes already
waiting for deletion count as freed, so consecutive runs do not evict twice for the same excess.
Objects used within the grace period are never evicted (a touch after the tombstone must be
seen, and the store throttles touches to one per hour).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from datetime import datetime

from ebs.core.digest import Digest
from ebs.gc.roots import TreeGraph
from ebs.meta.api import DomainPolicy, GcCacheEntry

__all__ = ["QuotaPlan", "plan_quota"]


@dataclass(frozen=True, slots=True)
class QuotaPlan:
    quota_bytes: int
    usage_bytes: int  # every object in the CAS
    pending_bytes: int  # already on their way out (tombstoned, or unreachable this run)
    high_water_bytes: int
    low_water_bytes: int
    evicted: tuple[Digest, ...]  # in eviction order (LRU)
    evicted_bytes: int
    rows: tuple[Digest, ...]  # cache entries referencing an evicted object: deleted first


def plan_quota(
    policy: DomainPolicy,
    sizes: Mapping[Digest, int],
    *,
    protected: Set[Digest],
    pending: Set[Digest],
    access: Mapping[Digest, datetime],
    not_after: datetime,
    entries: Iterable[GcCacheEntry],
    graph: TreeGraph,
) -> QuotaPlan | None:
    """The eviction plan, or None if the domain has no quota.

    `protected`: rooted objects (never evicted). `pending`: objects already leaving.
    `access`: effective last use. Only objects used at or before `not_after` are candidates.
    """
    if policy.quota_bytes is None:
        return None
    quota = policy.quota_bytes
    usage = sum(sizes.values())
    pending_bytes = sum(sizes[d] for d in pending if d in sizes)
    high, low = int(quota * policy.high_water), int(quota * policy.low_water)
    evicted: list[Digest] = []
    freed = 0
    if usage - pending_bytes > high:
        depth = graph.depths()
        parents = graph.parents()
        chosen: set[Digest] = set()

        def candidate(d: Digest) -> bool:
            return (
                d in sizes
                and d not in protected
                and d not in pending
                and d not in chosen
                and access[d] <= not_after
            )

        order = sorted(
            (d for d in sizes if candidate(d)), key=lambda d: (access[d], depth.get(d, 0), str(d))
        )
        for d in order:
            if usage - pending_bytes - freed <= low:
                break
            if d in chosen:
                continue
            # A tree goes with the part of its subtree that nothing else holds.
            unit = [d]
            chosen.add(d)
            todo = [d]
            while todo:
                for child in graph.children.get(todo.pop(), ()):
                    if candidate(child) and parents.get(child, set()) <= chosen:
                        chosen.add(child)
                        unit.append(child)
                        todo.append(child)
            evicted += unit
            freed += sum(sizes[x] for x in unit)
    gone = set(evicted)
    rows = tuple(
        e.key
        for e in entries
        if gone & graph.closure([*e.outputs, *([e.log] if e.log is not None else [])])
    )
    return QuotaPlan(quota, usage, pending_bytes, high, low, tuple(evicted), freed, rows)
