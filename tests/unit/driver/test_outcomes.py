"""Regression tests from the P0-15 review: build status, exit codes and stopping (R5, R6, R10)."""

from __future__ import annotations

from typing import Any

import pytest

import tests.helpers.driver as driver_helpers
from ebs.core.errors import ExitCode
from ebs.driver.retry import BackoffRetry
from ebs.meta.api import ResultManifest
from tests.helpers.driver import Env, Node

CHAIN = {"a": Node(), "b": Node(deps=(("a", "out"),))}


@pytest.fixture
def exit_zero_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tools that report FAIL in their results but exit 0 (common for simulators)."""
    original = driver_helpers.result_for

    def result_for(*args: Any, **kwargs: Any) -> ResultManifest:
        return original(*args, **kwargs).model_copy(update={"exit_code": 0})

    monkeypatch.setattr(driver_helpers, "result_for", result_for)


def _warm_failure(env: Env, nodes: dict[str, Node]) -> None:
    warm = env.executor()
    warm.script("a", "fail")
    env.run(env.plan(nodes), warm, cache_mode="write", keep_going=True)


# R5: the manifest's status decides, not the exit code.
@pytest.mark.usefixtures("exit_zero_failures")
def test_failed_status_with_exit_zero_is_failure(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "fail")
    outcome = env.run(env.plan(CHAIN), executor, keep_going=True)
    assert outcome.states == {"a": "failed", "b": "skipped"}
    assert executor.submitted == ["a"]


# R5
@pytest.mark.usefixtures("exit_zero_failures")
def test_cached_failure_with_exit_zero_blocks(env: Env) -> None:
    _warm_failure(env, {"a": Node()})
    executor = env.executor()
    outcome = env.run(env.plan(CHAIN), executor, cache_mode="read", keep_going=True)
    assert outcome.states == {"a": "cached", "b": "skipped"}
    assert executor.submitted == []


# R10: a build whose only action is a cached failure has failed.
def test_lone_cached_failure_fails_build(env: Env) -> None:
    _warm_failure(env, {"a": Node()})
    outcome = env.run(env.plan({"a": Node()}), env.executor(), cache_mode="read")
    assert outcome.states == {"a": "cached"}
    assert outcome.status == "failed"
    assert outcome.exit_code == ExitCode.ACTIONS_FAILED


# R5: a cached failure stops the build without -k, wherever it sits in the plan order.
@pytest.mark.parametrize("order", ["failure-first", "failure-last"])
def test_cached_failure_stops_build(env: Env, order: str) -> None:
    _warm_failure(env, {"a": Node()})
    nodes = {"a": Node(), "x": Node()} if order == "failure-first" else {"x": Node(), "a": Node()}
    executor = env.executor()
    outcome = env.run(env.plan(nodes), executor, cache_mode="read")
    assert executor.batches == []
    assert outcome.states == {"a": "cached", "x": "skipped"}
    assert outcome.exit_code == ExitCode.ACTIONS_FAILED


# R5, R6
def test_exhausted_infra_stops_build(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"))
    executor.script("r", "pass", running_polls=3)
    plan = env.plan({"a": Node(), "r": Node(), "s": Node(deps=(("r", "out"),))})
    outcome = env.run(plan, executor, max_retries=0)
    assert executor.submitted == ["a", "r"]
    assert outcome.states == {"a": "infra_failed", "r": "done", "s": "skipped"}
    assert outcome.exit_code == ExitCode.INFRA


# R5
def test_missing_input_stops_build(env: Env) -> None:
    plan = env.plan(
        {
            "a": Node(outputs={"out": True, "cov": True}, optional=frozenset({"cov"})),
            "b": Node(deps=(("a", "cov"),)),
            "r": Node(),
            "s": Node(deps=(("r", "out"),)),
        }
    )
    executor = env.executor()
    executor.script("a", "pass", omit={"cov"})
    executor.script("r", "pass", running_polls=3)
    outcome = env.run(plan, executor)
    assert executor.submitted == ["a", "r"]
    assert outcome.states["s"] == "skipped"
    assert outcome.status == "failed"


# R6: exit 3 only when retries were really used up; a stop caused by a test failure is exit 1.
def test_infra_after_stop_is_not_exit_3(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "fail", running_polls=0)
    executor.script("x", ("infra", "node_fail"), "pass", running_polls=1)
    outcome = env.run(env.plan({"a": Node(), "x": Node()}), executor)
    assert outcome.states == {"a": "failed", "x": "infra_failed"}
    assert executor.runs("x") == 1  # not retried: the build was stopping
    assert outcome.status == "failed"
    assert outcome.exit_code == ExitCode.ACTIONS_FAILED


# R6: a retry still waiting when the build stops is abandoned, keeping its infra_failed state.
def test_waiting_retry_abandoned_on_stop(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "fail", running_polls=2)
    executor.script("x", ("infra", "node_fail"), "pass")
    outcome = env.run(env.plan({"a": Node(), "x": Node()}), executor)
    assert outcome.states == {"a": "failed", "x": "infra_failed"}
    assert executor.runs("x") == 1
    assert outcome.status == "failed"


# R6: a single action retried after a backoff, with nothing else running meanwhile.
def test_lone_retry_after_backoff(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "preempted"), "pass")
    outcome = env.run(env.plan({"a": Node()}), executor, retry=BackoffRetry(base_s=7.0))
    assert outcome.states == {"a": "done"}
    assert 7.0 in env.clock.sleeps


# R6
def test_external_cancel_reason(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "vanished")
    outcome = env.run(env.plan({"a": Node()}), executor, max_retries=0)
    (row,) = env.store.list_actions(outcome.build)
    assert (row.state, row.infra_reason) == ("infra_failed", "other")


# R10: keys known at plan time are recorded from the start, even for actions that never run.
def test_rows_keyed_from_start(env: Env) -> None:
    plan = env.plan({"a": Node(outputs={"lib": False}), "b": Node(deps=(("a", "lib"),))})
    executor = env.executor()
    executor.script("a", "fail")
    outcome = env.run(plan, executor, keep_going=True)
    rows = {r.action_id: r for r in env.store.list_actions(outcome.build)}
    assert rows["b"].state == "skipped"
    assert rows["b"].key == plan.action("b").key
    assert rows["b"].key is not None


# R5 (found by the warm-cache property test): a cached failure late in the plan stops the build
# after an earlier action was passed over waiting for a producer; it must end skipped, not hang.
def test_stop_found_late_in_pass_skips_waiting_actions(env: Env) -> None:
    nodes = {
        "a": Node(outputs={"out": False}),
        "b": Node(deps=(("a", "out"),), outputs={"out": False}),
        "c": Node(deps=(("a", "out"), ("b", "out")), outputs={"out": False}),
        "f": Node(),
    }
    first = env.executor()
    first.script("f", "fail")
    env.run(env.plan(nodes), first, cache_mode="write")  # b and c were skipped: not cached

    executor = env.executor()
    outcome = env.run(env.plan(nodes), executor, cache_mode="write")
    assert executor.batches == []
    assert outcome.states == {"a": "cached", "b": "skipped", "c": "skipped", "f": "cached"}
    assert outcome.status == "failed"
