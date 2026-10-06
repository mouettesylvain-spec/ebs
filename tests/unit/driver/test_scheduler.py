from __future__ import annotations

import pytest

from ebs.core.digest import hash_bytes
from ebs.flow.model import Resources
from tests.helpers.driver import Env, Node

CHAIN = {"a": Node(), "b": Node(deps=(("a", "out"),))}


# R1
def test_ready_only_when_inputs_known(env: Env) -> None:
    plan = env.plan(CHAIN)
    assert plan.action("a").key is not None
    assert plan.action("b").key is None  # waits for a's content digest
    executor = env.executor()
    executor.script("a", "pass", running_polls=3, outputs={"out": b"A bytes"})

    outcome = env.run(plan, executor)

    assert outcome.states == {"a": "done", "b": "done"}
    assert executor.submitted == ["a", "b"]
    assert executor.finished == ["a", "b"]  # b was only submitted after a finished
    b = executor.submitted_specs[1]  # as loaded by the "runner" from the plan in the CAS
    assert b.input("out/a").id == hash_bytes(b"A bytes")
    assert b.key == outcome.plan.action("b").key is not None


# R1: an action whose producer is nondeterministic has a key early, but still waits for it.
def test_nondeterministic_input_waits_for_producer(env: Env) -> None:
    plan = env.plan({"a": Node(outputs={"lib": False}), "b": Node(deps=(("a", "lib"),))})
    assert plan.action("b").key is not None
    executor = env.executor()
    executor.script("a", "pass", running_polls=3)
    outcome = env.run(plan, executor)
    assert executor.finished == ["a", "b"]
    assert outcome.states == {"a": "done", "b": "done"}


# R3
def test_cache_hit_skips_submission(env: Env) -> None:
    plan = env.plan(CHAIN)
    first = env.run(plan, env.executor(), cache_mode="write")
    assert first.states == {"a": "done", "b": "done"}

    executor = env.executor()
    second = env.run(plan, executor, cache_mode="read")

    assert executor.batches == []
    assert executor.polls == 0
    assert second.states == {"a": "cached", "b": "cached"}
    assert second.status == "passed"
    rows = {r.action_id: r for r in env.store.list_actions(second.build)}
    assert all(r.cached and r.state == "cached" for r in rows.values())
    assert rows["b"].key == first.plan.action("b").key
    # The cached manifest is recorded on the new build too (ebs logs / provenance).
    assert env.store.get_result(second.build, "b") == env.store.get_result(first.build, "b")


# R4
@pytest.mark.parametrize(("same_bytes", "b_reruns"), [(True, False), (False, True)])
def test_early_cutoff(env: Env, same_bytes: bool, b_reruns: bool) -> None:
    first = env.executor()
    first.script("a", "pass", outputs={"out": b"report v1"})
    env.run(env.plan(CHAIN), first, cache_mode="write")

    # a's input changes (a comment-only edit, say), so a reruns...
    changed = env.plan({"a": Node(source="edited"), "b": Node(deps=(("a", "out"),))})
    executor = env.executor()
    executor.script("a", "pass", outputs={"out": b"report v1" if same_bytes else b"report v2"})
    outcome = env.run(changed, executor, cache_mode="write")

    # ...but if its output bytes are unchanged, b's key is too and b hits the cache.
    assert executor.submitted == (["a", "b"] if b_reruns else ["a"])
    assert outcome.states == {"a": "done", "b": "done" if b_reruns else "cached"}


# R5
def test_failure_blocks_dependents(env: Env) -> None:
    plan = env.plan(
        {
            "a": Node(),
            "b": Node(deps=(("a", "out"),)),
            "c": Node(deps=(("b", "out"),)),
            "x": Node(),
        }
    )
    executor = env.executor()
    executor.script("a", "fail")
    outcome = env.run(plan, executor, keep_going=True)

    assert outcome.states == {"a": "failed", "b": "skipped", "c": "skipped", "x": "done"}
    assert "b" not in executor.submitted
    assert "c" not in executor.submitted
    assert outcome.status == "failed"
    assert outcome.exit_code == 1
    rows = {r.action_id: r.state for r in env.store.list_actions(outcome.build)}
    assert rows == outcome.states


# R5
def test_keep_going(env: Env) -> None:
    plan = env.plan(
        {"a": Node(), "x": Node(), "y": Node(deps=(("x", "out"),)), "z": Node(deps=(("y", "out"),))}
    )
    executor = env.executor()
    executor.script("a", "fail")
    executor.script("x", "pass", running_polls=2)
    outcome = env.run(plan, executor, keep_going=True)
    assert outcome.states == {"a": "failed", "x": "done", "y": "done", "z": "done"}
    assert outcome.exit_code == 1


# R5
def test_stop_on_first_failure(env: Env) -> None:
    plan = env.plan({"a": Node(), "r": Node(), "s": Node(deps=(("r", "out"),))})
    executor = env.executor()
    executor.script("a", "fail")
    executor.script("r", "pass", running_polls=3)
    outcome = env.run(plan, executor)

    assert executor.submitted == ["a", "r"]  # s became ready after the failure: not started
    assert outcome.states == {"a": "failed", "r": "done", "s": "skipped"}  # r was let finish
    assert outcome.status == "failed"
    assert outcome.exit_code == 1


# R5: a test failure served from the cache is still a failure.
def test_cached_failure_blocks_dependents(env: Env) -> None:
    plan = env.plan(CHAIN)
    first = env.executor()
    first.script("a", "fail")
    env.run(plan, first, cache_mode="write")

    executor = env.executor()
    outcome = env.run(plan, executor, cache_mode="read")
    assert executor.submitted == []
    assert outcome.states == {"a": "cached", "b": "skipped"}
    assert outcome.status == "failed"
    assert outcome.exit_code == 1

    # --rerun-failed bypasses the failed cache entry (R2).
    rerun = env.executor()
    again = env.run(plan, rerun, cache_mode="read", rerun_failed=True)
    assert rerun.submitted == ["a", "b"]
    assert again.states == {"a": "done", "b": "done"}


# R7
def test_batching_rules(env: Env) -> None:
    small = Resources(cpus=1)
    big = Resources(cpus=8)
    nodes = {
        **{f"sim[{i}]": Node(resources=small) for i in range(5)},
        **{f"sim[big{i}]": Node(resources=big) for i in range(2)},
        "sim[lic]": Node(resources=small, licenses={"msimhdlsim": 1}),
        "lint": Node(resources=small),
    }
    executor = env.executor()
    outcome = env.run(env.plan(nodes), executor, max_batch=2)

    assert outcome.status == "passed"
    groups = sorted(sorted(a.action_id for a in b.actions) for b in executor.batches)
    assert groups == [
        ["lint"],
        ["sim[0]", "sim[1]"],
        ["sim[2]", "sim[3]"],
        ["sim[4]"],
        ["sim[big0]", "sim[big1]"],
        ["sim[lic]"],
    ]
    for batch in executor.batches:
        assert len({(a.step, a.resources, tuple(a.licenses.items())) for a in batch.actions}) == 1
        assert batch.build == outcome.build
        assert batch.domain == "test"


# R9
def test_adaptive_polling_single_call(env: Env) -> None:
    plan = env.plan({"a": Node(), "b": Node(), "c": Node()})
    executor = env.executor()
    for name in ("a", "b", "c"):
        executor.script(name, "pass", running_polls=5)
    outcome = env.run(plan, executor, poll_min_s=1.0, poll_max_s=4.0)

    assert outcome.status == "passed"
    # One poll per loop iteration, covering every in-flight job.
    assert executor.poll_sizes == [3] * 6
    # Poll 1 sees "running" (a change), polls 2-5 see nothing new, poll 6 sees "done".
    assert env.clock.sleeps == [1.0, 1.0, 2.0, 4.0, 4.0, 4.0]


# R2: --rerun-failed in `write` mode replaces the cached failure, so the next build reuses the pass.
def test_rerun_failed_replaces_cached_failure(env: Env) -> None:
    plan = env.plan({"a": Node()})
    first = env.executor()
    first.script("a", "fail")
    env.run(plan, first, cache_mode="write")

    rerun = env.executor()  # the flaky test passes this time
    again = env.run(plan, rerun, cache_mode="write", rerun_failed=True)
    assert again.states == {"a": "done"}
    key = plan.action("a").key
    assert key is not None
    cached = env.store.cache_get("test", key)
    assert cached is not None
    assert cached.status == "passed"

    later = env.executor()
    third = env.run(plan, later, cache_mode="read")
    assert later.submitted == []
    assert third.states == {"a": "cached"}
    assert third.status == "passed"


# R2: in `read` mode a rerun never writes the cache, not even to replace a failure.
def test_rerun_failed_read_mode_keeps_cache(env: Env) -> None:
    plan = env.plan({"a": Node()})
    first = env.executor()
    first.script("a", "fail")
    env.run(plan, first, cache_mode="write")
    env.run(plan, env.executor(), cache_mode="read", rerun_failed=True)
    key = plan.action("a").key
    assert key is not None
    cached = env.store.cache_get("test", key)
    assert cached is not None
    assert cached.status == "failed"
