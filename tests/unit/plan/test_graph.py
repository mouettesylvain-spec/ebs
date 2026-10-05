"""Tests for the step graph: topological order, cycles, target closure (task P0-08 R2, R3)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.errors import PlanError
from ebs.plan.graph import StepGraph


def graph(nodes: str, edges: list[tuple[str, str]]) -> StepGraph:
    g = StepGraph(nodes.split())
    for producer, consumer in edges:
        g.add_edge(producer, consumer)
    return g


def test_topo_order_producers_first_and_stable() -> None:
    g = graph("sim lint elab compile", [("compile", "elab"), ("elab", "sim")])
    # Ties keep declaration order; producers always come before consumers.
    assert g.topo_order() == ["lint", "compile", "elab", "sim"]


def test_duplicate_edges_are_ignored() -> None:
    g = graph("a b", [("a", "b"), ("a", "b")])
    assert g.deps("b") == ("a",)
    assert g.topo_order() == ["a", "b"]


# R2
def test_cycle_reported() -> None:
    g = graph("compile elab sim lint", [("compile", "elab"), ("elab", "sim"), ("sim", "compile")])
    with pytest.raises(PlanError) as exc:
        g.topo_order()
    message = str(exc.value)
    assert "compile -> elab -> sim -> compile" in message
    assert "lint" not in message


# R2
def test_self_reference_is_a_cycle() -> None:
    g = graph("a b", [("a", "b"), ("b", "b")])
    with pytest.raises(PlanError, match=r"b -> b"):
        g.topo_order()


# R3
def test_closure_is_targets_plus_transitive_dependencies() -> None:
    g = graph("a b c d e", [("a", "b"), ("b", "c"), ("d", "c"), ("a", "e")])
    assert g.closure(["c"]) == {"a", "b", "c", "d"}
    assert g.closure(["e"]) == {"a", "e"}
    assert g.closure([]) == {"a", "b", "c", "d", "e"}


def test_unknown_node_is_an_internal_error() -> None:
    g = graph("a", [])
    with pytest.raises(KeyError):
        g.add_edge("a", "zz")


@given(
    st.integers(min_value=1, max_value=8).flatmap(
        lambda n: st.tuples(
            st.just(n),
            st.lists(st.tuples(st.integers(0, n - 1), st.integers(0, n - 1)), max_size=20),
        )
    )
)
def test_topo_order_respects_every_edge_of_a_dag(case: tuple[int, list[tuple[int, int]]]) -> None:
    n, raw = case
    edges = [(f"n{min(a, b)}", f"n{max(a, b)}") for a, b in raw if a != b]  # acyclic
    g = graph(" ".join(f"n{i}" for i in reversed(range(n))), edges)
    order = g.topo_order()
    assert sorted(order) == sorted(f"n{i}" for i in range(n))
    for producer, consumer in edges:
        assert order.index(producer) < order.index(consumer)
