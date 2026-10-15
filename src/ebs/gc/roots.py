"""GC roots and the tree graph they are closed over (P1-09 R2, R3).

Roots are release outputs, the plan, outputs and logs of pinned builds, and the objects of live
leases (`GcStore.gc_roots`). A root tree protects everything below it, so every set of digests
GC keeps is closed over `TreeGraph`: tree manifest -> the child manifests and blobs it names.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Protocol

from ebs.core.digest import Digest
from ebs.core.tree import TreeManifest
from ebs.meta.api import GcRoots

__all__ = ["ManifestReader", "TreeGraph", "read_graph", "root_set"]


class ManifestReader(Protocol):
    def get_tree(self, d: Digest) -> TreeManifest: ...


class TreeGraph:
    """Parent -> children edges between CAS objects (a Merkle DAG: no cycles)."""

    def __init__(self, children: Mapping[Digest, Iterable[Digest]]) -> None:
        self.children: dict[Digest, tuple[Digest, ...]] = {
            parent: tuple(dict.fromkeys(kids)) for parent, kids in children.items()
        }
        self._parents: dict[Digest, set[Digest]] | None = None

    def closure(self, seeds: Iterable[Digest]) -> set[Digest]:
        """`seeds` and everything below them."""
        found = set(seeds)
        todo = list(found)
        while todo:
            for child in self.children.get(todo.pop(), ()):
                if child not in found:
                    found.add(child)
                    todo.append(child)
        return found

    def parents(self) -> dict[Digest, set[Digest]]:
        """Child -> the trees that contain it."""
        if self._parents is None:
            parents: dict[Digest, set[Digest]] = {}
            for parent, kids in self.children.items():
                for child in kids:
                    parents.setdefault(child, set()).add(parent)
            self._parents = parents
        return self._parents

    def depths(self) -> dict[Digest, int]:
        """Longest distance from a top-level tree (0 = no parent): parents sort before their
        children when ordered by depth."""
        parents = self.parents()
        depth: dict[Digest, int] = {}
        nodes = set(self.children) | set(parents)
        for start in nodes:
            stack = [start]
            while stack:
                node = stack[-1]
                if node in depth:
                    stack.pop()
                    continue
                pending = [p for p in parents.get(node, ()) if p not in depth]
                if pending:
                    stack.extend(pending)
                    continue
                stack.pop()
                depth[node] = 1 + max((depth[p] for p in parents.get(node, ())), default=-1)
        return depth


def read_graph(cas: ManifestReader, trees: Iterable[Digest]) -> TreeGraph:
    """The graph of `trees` (manifest digests), read from the CAS. An unreadable manifest raises
    CasError: GC cannot know what it protects, so it must not sweep."""
    children: dict[Digest, list[Digest]] = {}
    for tree in trees:
        children[tree] = [e.digest for e in cas.get_tree(tree).entries if e.digest is not None]
    return TreeGraph(children)


def root_set(roots: GcRoots) -> frozenset[Digest]:
    """Every root digest (I15): releases, pins and live leases."""
    return roots.released | roots.pinned | roots.leased
