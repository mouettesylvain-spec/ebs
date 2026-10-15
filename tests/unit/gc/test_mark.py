"""Mark phase (P1-09 R3, invariant I15): closure over trees and over action-cache entries."""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import given
from hypothesis import strategies as st

from ebs.core.digest import Digest, hash_bytes
from ebs.gc.mark import mark
from ebs.gc.roots import TreeGraph
from ebs.meta.api import GcCacheEntry

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def node(i: int) -> Digest:
    return hash_bytes(f"node-{i}".encode())


@st.composite
def graphs(draw: st.DrawFn) -> tuple[int, dict[int, set[int]]]:
    """A Merkle DAG on nodes 0..n-1: a node may only contain nodes with a larger index."""
    n = draw(st.integers(min_value=1, max_value=25))
    children: dict[int, set[int]] = {}
    for i in range(n - 1):
        kids = draw(st.sets(st.integers(min_value=i + 1, max_value=n - 1), max_size=4))
        if kids:
            children[i] = kids
    return n, children


def as_graph(children: dict[int, set[int]]) -> TreeGraph:
    return TreeGraph({node(i): [node(k) for k in kids] for i, kids in children.items()})


def reach(children: dict[int, set[int]], seeds: set[int]) -> set[int]:
    """Reference closure: repeat until nothing new is found."""
    found = set(seeds)
    while True:
        more = {k for i in found for k in children.get(i, ())} - found
        if not more:
            return found
        found |= more


# R3
@given(graphs(), st.data())
def test_tree_closure(g: tuple[int, dict[int, set[int]]], data: st.DataObject) -> None:
    n, children = g
    roots = data.draw(st.sets(st.integers(min_value=0, max_value=n - 1), max_size=5))
    marked = mark([node(i) for i in roots], [], as_graph(children))
    expected = {node(i) for i in reach(children, roots)}
    assert marked.reachable == expected
    assert marked.entries == frozenset()
    # A marked tree never loses a child to the sweep (I15): its whole subtree is marked.
    for i, kids in children.items():
        if node(i) in marked.reachable:
            assert {node(k) for k in kids} <= marked.reachable


# R3: a cache entry is kept iff every output is reachable from a root; keeping it keeps its log,
# which may in turn complete another entry (fixpoint).
@given(graphs(), st.data())
def test_cache_entry_closure(g: tuple[int, dict[int, set[int]]], data: st.DataObject) -> None:
    n, children = g
    index = st.integers(min_value=0, max_value=n - 1)
    roots = data.draw(st.sets(index, max_size=4))
    raw = data.draw(st.lists(st.tuples(st.sets(index, max_size=3), st.none() | index), max_size=8))
    entries = [
        GcCacheEntry(
            hash_bytes(f"key-{j}".encode()),
            T0,
            tuple(sorted((node(i) for i in outs), key=str)),
            None if log is None else node(log),
        )
        for j, (outs, log) in enumerate(raw)
    ]
    marked = mark([node(i) for i in roots], entries, as_graph(children))

    kept: set[int] = set()
    reached = reach(children, roots)
    while True:
        now_kept = {j for j, (outs, _) in enumerate(raw) if outs and outs <= reached}
        logs = {log for j, (_, log) in enumerate(raw) if j in now_kept and log is not None}
        grown = reach(children, reached | logs)
        if now_kept == kept and grown == reached:
            break
        kept, reached = now_kept, grown
    assert marked.entries == {entries[j].key for j in kept}
    assert marked.reachable == {node(i) for i in reached}


# R3: entries without outputs are never kept by marking (only by their TTL).
def test_entry_without_outputs_is_not_marked() -> None:
    log = node(1)
    entry = GcCacheEntry(hash_bytes(b"k"), T0, (), log)
    marked = mark([log], [entry], TreeGraph({}))
    assert marked.entries == frozenset()
    assert marked.reachable == {log}
