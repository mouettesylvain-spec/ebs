"""Random DAGs with random outcomes (Hypothesis): every build ends in a consistent state."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ebs.meta.api import TERMINAL_STATES
from tests.helpers.driver import Node, Outcome, make_env

_CHOICES: list[Outcome] = [
    "pass",
    "pass",
    "pass",
    "fail",
    ("infra", "node_fail"),
    ("infra", "oom"),
]
OUTCOMES: st.SearchStrategy[Outcome] = st.sampled_from(_CHOICES)


@st.composite
def dags(draw: st.DrawFn) -> tuple[dict[str, Node], dict[str, list[Outcome]]]:
    n = draw(st.integers(min_value=1, max_value=10))
    nodes: dict[str, Node] = {}
    scripts: dict[str, list[Outcome]] = {}
    for i in range(n):
        name = f"s{draw(st.integers(0, 2))}[{i}]"  # a few steps, so batching kicks in
        earlier = list(nodes)
        producers = (
            draw(st.lists(st.sampled_from(earlier), unique=True, max_size=3)) if earlier else []
        )
        deterministic = draw(st.booleans())
        nodes[name] = Node(
            deps=tuple((p, "out") for p in producers), outputs={"out": deterministic}
        )
        scripts[name] = draw(st.lists(OUTCOMES, min_size=1, max_size=3))
    return nodes, scripts


def _ancestors(nodes: dict[str, Node], name: str) -> set[str]:
    seen: set[str] = set()
    todo = [p for p, _ in nodes[name].deps]
    while todo:
        p = todo.pop()
        if p not in seen:
            seen.add(p)
            todo.extend(q for q, _ in nodes[p].deps)
    return seen


# R1, R5, R6 (task budget: 500 examples in < 20 s). max_examples is fixed on purpose: the nightly
# profile's 5000 would take minutes; the derandomized CI profile keeps it reproducible.
@pytest.mark.slow
@settings(max_examples=500, deadline=None)
@given(
    dag=dags(),
    keep_going=st.booleans(),
    max_retries=st.integers(0, 2),
    max_batch=st.integers(1, 4),
    polls=st.integers(1, 3),
    warm=st.booleans(),
)
def test_random_dags(
    dag: tuple[dict[str, Node], dict[str, list[Outcome]]],
    keep_going: bool,
    max_retries: int,
    max_batch: int,
    polls: int,
    warm: bool,
) -> None:
    nodes, scripts = dag
    with tempfile.TemporaryDirectory() as tmp:
        env = make_env(Path(tmp))
        plan = env.plan(nodes)
        builds = 2 if warm else 1  # warm: the second build runs against the first one's cache
        for _ in range(builds):
            executor = env.executor()  # asserts no action is submitted before its producers
            for name, outcomes in scripts.items():
                executor.script(name, *outcomes, running_polls=polls)
            outcome = env.run(
                plan,
                executor,
                cache_mode="write",
                keep_going=keep_going,
                max_retries=max_retries,
                max_batch=max_batch,
            )
        rows = {r.action_id: r.state for r in env.store.list_actions(outcome.build)}
        cached_failures = {
            a
            for a, state in outcome.states.items()
            if state == "cached"
            and (result := env.store.get_result(outcome.build, a)) is not None
            and result.status == "failed"
        }

    states = outcome.states
    assert rows == states
    # Every action ends in a terminal state.
    assert set(states) == set(nodes)
    assert set(states.values()) <= TERMINAL_STATES
    assert "cancelled" not in states.values()
    # cached + run == every action that was not skipped.
    ran = {s for s in states if s in executor.submitted}
    cached = {s for s, st_ in states.items() if st_ == "cached"}
    skipped = {s for s, st_ in states.items() if st_ == "skipped"}
    assert ran | cached == set(nodes) - skipped
    assert not ran & skipped
    assert not ran & cached  # a cache hit is never also submitted
    # An action whose producer did not succeed is skipped.
    ok = {s for s, st_ in states.items() if st_ == "done" or s in cached - cached_failures}
    for name, node in nodes.items():
        if any(p not in ok for p, _ in node.deps):
            assert states[name] == "skipped"
        if keep_going and states[name] == "skipped":
            assert _ancestors(nodes, name) - ok
    # Retries: never more attempts than 1 + max_retries.
    assert all(executor.runs(s) <= 1 + max_retries for s in nodes)
    # Build status: any failure fails the build; exit 3 only with an infra failure in it.
    bad = {"failed", "infra_failed", "skipped"} & set(states.values()) or cached_failures
    if bad:
        assert outcome.status in {"failed", "infra_failed"}
    else:
        assert outcome.status == "passed"
    if outcome.status == "infra_failed":
        assert "infra_failed" in states.values()
    if keep_going and "infra_failed" in states.values():
        assert outcome.status == "infra_failed"  # with -k every infra failure exhausted retries
