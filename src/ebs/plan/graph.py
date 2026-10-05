"""Step-level DAG: deterministic topological order, cycle reporting, target closure.

Nodes are step names in declaration order; an edge `producer -> consumer` means the consumer
references one of the producer's outputs. The order is stable: among steps whose dependencies
are satisfied, the earliest declared comes first, so the same flow always plans the same way.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Sequence

from ebs.core.errors import PlanError

__all__ = ["StepGraph"]


class StepGraph:
    def __init__(self, nodes: Iterable[str]) -> None:
        self._index: dict[str, int] = {}
        for node in nodes:
            self._index.setdefault(node, len(self._index))
        self._deps: dict[str, list[str]] = {n: [] for n in self._index}

    def add_edge(self, producer: str, consumer: str) -> None:
        """`consumer` depends on `producer`; both must be nodes (KeyError otherwise)."""
        for node in (producer, consumer):
            if node not in self._index:
                raise KeyError(node)
        if producer not in self._deps[consumer]:
            self._deps[consumer].append(producer)

    def deps(self, node: str) -> tuple[str, ...]:
        """Direct dependencies of `node`, in the order they were added."""
        return tuple(self._deps[node])

    def topo_order(self) -> list[str]:
        """Producers before consumers; ties broken by declaration order. PlanError on a cycle."""
        consumers: dict[str, list[str]] = {n: [] for n in self._index}
        waiting = {n: len(deps) for n, deps in self._deps.items()}
        for node, deps in self._deps.items():
            for dep in deps:
                consumers[dep].append(node)
        ready = [(self._index[n], n) for n, count in waiting.items() if count == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            _, node = heapq.heappop(ready)
            order.append(node)
            for consumer in consumers[node]:
                waiting[consumer] -= 1
                if waiting[consumer] == 0:
                    heapq.heappush(ready, (self._index[consumer], consumer))
        if len(order) != len(self._index):
            raise PlanError(self._cycle_message(set(self._index) - set(order)))
        return order

    def _cycle_message(self, stuck: set[str]) -> str:
        # Every stuck node has a stuck dependency (else it would have been ordered), so
        # walking dependencies from the first declared stuck node must revisit a node.
        node = min(stuck, key=self._index.__getitem__)
        path: list[str] = []
        seen: dict[str, int] = {}
        while node not in seen:
            seen[node] = len(path)
            path.append(node)
            node = next(d for d in self._deps[node] if d in stuck)
        cycle = path[seen[node] :]
        cycle.reverse()  # walked consumer -> producer; report producer -> consumer
        first = cycle.index(min(cycle, key=self._index.__getitem__))
        cycle = [*cycle[first:], *cycle[:first], cycle[first]]
        return (
            f"dependency cycle between steps: {' -> '.join(cycle)} (each step's outputs are "
            "used by the next); break the cycle by removing one of these output references"
        )

    def closure(self, targets: Sequence[str]) -> set[str]:
        """`targets` plus everything they depend on, transitively; all nodes if empty."""
        if not targets:
            return set(self._index)
        result: set[str] = set()
        stack = list(targets)
        while stack:
            node = stack.pop()
            if node not in result:
                result.add(node)
                stack.extend(self._deps[node])
        return result
