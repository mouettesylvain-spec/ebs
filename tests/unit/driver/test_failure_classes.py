"""Invariant I12: infrastructure failures are retried and never cached; test failures are cached."""

from __future__ import annotations

import dataclasses

from ebs.driver.retry import BackoffRetry, RetryDecision
from ebs.flow.model import Resources
from ebs.meta.api import InfraReason
from ebs.plan.types import ActionSpec
from tests.helpers.driver import Env, Node

CHAIN = {"a": Node(), "b": Node(deps=(("a", "out"),))}


# R6 / I12
def test_infra_not_cached_and_retried(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "node_fail"), ("infra", "preempted"), "pass")
    retry = BackoffRetry(max_retries=2, base_s=30.0, factor=2.0, max_s=600.0)
    outcome = env.run(env.plan(CHAIN), executor, cache_mode="write", retry=retry)

    assert outcome.states == {"a": "done", "b": "done"}
    assert executor.submitted == ["a", "a", "a", "b"]
    rows = {r.action_id: r for r in env.store.list_actions(outcome.build)}
    assert rows["a"].attempts == 3
    key = outcome.plan.action("a").key
    assert key is not None
    cached = env.store.cache_get("test", key)
    assert cached is not None
    assert cached.status == "passed"  # only the real result
    # Backoff: the retries waited at least 30 s and then 60 s of (fake) time.
    assert sum(env.clock.sleeps) >= 90.0


# R6 / I12
def test_retries_exhausted_exit_3(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "oom"))
    outcome = env.run(env.plan(CHAIN), executor, cache_mode="write", max_retries=2)

    assert executor.submitted == ["a", "a", "a"]  # first try + 2 retries
    assert outcome.states == {"a": "infra_failed", "b": "skipped"}
    assert outcome.status == "infra_failed"
    assert outcome.exit_code == 3
    key = outcome.plan.action("a").key
    assert key is not None
    assert env.store.cache_get("test", key) is None  # never cached
    assert env.store.get_result(outcome.build, "a") is None
    row = {r.action_id: r for r in env.store.list_actions(outcome.build)}["a"]
    assert (row.state, row.infra_reason) == ("infra_failed", "oom")


# I12: the other half — a test failure is a valid, cached result and is not retried.
def test_test_failure_cached_not_retried(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "fail")
    outcome = env.run(env.plan({"a": Node()}), executor, cache_mode="write")
    assert executor.submitted == ["a"]
    key = outcome.plan.action("a").key
    assert key is not None
    cached = env.store.cache_get("test", key)
    assert cached is not None
    assert cached.status == "failed"


# R6: the retry hook (P1-05: OOM => more memory) can change resources, never the key.
def test_retry_hook_can_change_resources(env: Env) -> None:
    class MoreMemory:
        def decide(
            self, spec: ActionSpec, attempt: int, reason: InfraReason
        ) -> RetryDecision | None:
            if attempt > 1:
                return None
            assert reason == "oom"
            return RetryDecision(delay_s=0.0, resources=Resources(mem="16G"))

    executor = env.executor()
    executor.script("a", ("infra", "oom"), "pass")
    outcome = env.run(
        env.plan({"a": Node(resources=Resources(mem="4G"))}), executor, retry=MoreMemory()
    )

    first, second = executor.submitted_specs
    assert first.resources.mem == 4 << 30
    assert second.resources.mem == 16 << 30  # the runner sees it: it came from the CAS plan
    assert second.key == first.key
    assert dataclasses.replace(second, resources=first.resources) == first
    assert outcome.states == {"a": "done"}


# R6: an exhausted infra failure blocks only its own branch with -k.
def test_infra_failure_keep_going(env: Env) -> None:
    executor = env.executor()
    executor.script("a", ("infra", "license"))
    plan = env.plan({**CHAIN, "x": Node()})
    outcome = env.run(plan, executor, keep_going=True, max_retries=0)
    assert outcome.states == {"a": "infra_failed", "b": "skipped", "x": "done"}
    assert outcome.exit_code == 3
