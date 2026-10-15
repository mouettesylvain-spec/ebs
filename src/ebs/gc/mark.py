"""Mark phase (P1-09 R3, I15): what the roots reach, through trees and action-cache entries.

A root tree marks all its child manifests and blobs. An action-cache entry whose outputs are all
marked is kept as well (its result is protected anyway, so dropping the row gains nothing), and
keeping it marks its log, which may complete another entry: marking repeats until stable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ebs.core.digest import Digest
from ebs.gc.roots import TreeGraph
from ebs.meta.api import GcCacheEntry

__all__ = ["Marked", "mark"]


@dataclass(frozen=True, slots=True)
class Marked:
    reachable: frozenset[Digest]  # objects reachable from the roots
    entries: frozenset[Digest]  # keys of the cache entries kept by marking


def mark(roots: Iterable[Digest], entries: Iterable[GcCacheEntry], graph: TreeGraph) -> Marked:
    """Close `roots` over `graph` and over the cache entries whose outputs they all reach.

    An entry without outputs is never kept by marking: only its TTL keeps it.
    """
    reachable = graph.closure(roots)
    pending = [e for e in entries if e.outputs]
    kept: set[Digest] = set()
    changed = True
    while changed:
        changed = False
        rest = []
        for entry in pending:
            if all(d in reachable for d in entry.outputs):
                kept.add(entry.key)
                if entry.log is not None and entry.log not in reachable:
                    reachable |= graph.closure([entry.log])
                    changed = True
            else:
                rest.append(entry)
        pending = rest
    return Marked(frozenset(reachable), frozenset(kept))
