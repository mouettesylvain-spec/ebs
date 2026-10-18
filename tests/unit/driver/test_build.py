from __future__ import annotations

import dataclasses
import os
import signal
from typing import Any

import pytest

from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import CasError, ExitCode
from ebs.driver.build import action_row
from ebs.gc.access import put_registered
from ebs.plan.planfile import plan_digest
from ebs.plan.types import FlowInfo, GitInfo, RuleRef, ToolchainRef
from tests.helpers.driver import Env, Node


# R10
def test_build_record(env: Env) -> None:
    flow = FlowInfo(
        "flows/cpu/flow.yaml", GitInfo("git@example.invalid:cpu/flow.git", "c" * 40, True)
    )
    plan = env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))}, flow=flow)
    outcome = env.run(plan, env.executor(), cache_mode="write", ci_job="pipeline-7")

    view = env.store.get_build(outcome.build)
    assert view.uuid == outcome.uuid
    assert view.plan_digest == plan_digest(plan)  # the plan as planned, before refinement
    assert env.cas.has(view.plan_digest)
    assert (view.flow_repo, view.flow_commit, view.flow_dirty) == (
        "git@example.invalid:cpu/flow.git",
        "c" * 40,
        True,
    )
    assert (view.user_name, view.ci_job, view.cache_mode) == ("alice", "pipeline-7", "write")
    assert (view.domain, view.project) == ("test", "demo")
    assert view.status == "passed"
    assert view.finished_at is not None
    assert view.action_counts == {"done": 2}
    assert outcome.events_path.is_file()


# P1-09 follow-up: the refined final plan is stored, registered and recorded with the build, so
# a pin roots it; a CAS failure there still finishes the build (without it).
def test_final_plan_recorded(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))})
    outcome = env.run(plan, env.executor())
    view = env.store.get_build(outcome.build)
    assert view.final_plan_digest == plan_digest(outcome.plan)
    assert view.final_plan_digest != view.plan_digest  # b's key was filled in
    assert env.cas.has(view.final_plan_digest)
    env.store.pin_build(outcome.build, by="alice")
    roots = env.store.gc_roots("test", env.clock.now())
    assert {view.plan_digest, view.final_plan_digest} <= roots.pinned

    calls: list[tuple[Any, ...]] = []

    def refuse(*args: Any) -> Digest:
        calls.append(args)
        if len(calls) > 1:  # start_build stores the plan; the final plan comes second
            raise CasError("disk full")
        return put_registered(*args)

    monkeypatch.setattr("ebs.driver.build.put_registered", refuse)
    failed = env.run(env.plan({"c": Node()}), env.executor())
    view = env.store.get_build(failed.build)
    assert (view.status, view.final_plan_digest) == ("passed", None)


# R10: a flow outside git is recorded with empty repo/commit, not dirty.
def test_build_record_without_git(env: Env) -> None:
    outcome = env.run(env.plan({"a": Node()}), env.executor())
    view = env.store.get_build(outcome.build)
    assert (view.flow_repo, view.flow_commit, view.flow_dirty) == ("", "", False)
    assert view.ci_job is None


# R5, R6, R10
@pytest.mark.parametrize(
    ("scripts", "status", "code"),
    [
        ({}, "passed", ExitCode.OK),
        ({"a": ["fail"]}, "failed", ExitCode.ACTIONS_FAILED),
        ({"a": [("infra", "oom")]}, "infra_failed", ExitCode.INFRA),
        ({"a": ["fail"], "x": [("infra", "oom")]}, "infra_failed", ExitCode.INFRA),
    ],
)
def test_exit_codes(
    env: Env, scripts: dict[str, list[object]], status: str, code: ExitCode
) -> None:
    executor = env.executor()
    for action_id, outcomes in scripts.items():
        executor.script(action_id, *outcomes)  # type: ignore[arg-type]
    outcome = env.run(
        env.plan({"a": Node(), "x": Node()}), executor, keep_going=True, max_retries=0
    )
    assert (outcome.status, outcome.exit_code) == (status, code)
    assert env.store.get_build(outcome.build).status == status


# R8
def test_sigint_cancels(env: Env) -> None:
    before = signal.getsignal(signal.SIGINT)

    def interrupt(poll: int) -> None:
        if poll == 2:
            os.kill(os.getpid(), signal.SIGINT)

    executor = env.executor(on_poll=interrupt)
    executor.script("a", "hang")
    executor.script("q", "pass", running_polls=1)
    plan = env.plan({"a": Node(), "q": Node(), "b": Node(deps=(("a", "out"),))})
    outcome = env.run(plan, executor, handle_sigint=True)

    assert executor.cancelled == ["a"]
    assert outcome.states == {"a": "cancelled", "b": "cancelled", "q": "done"}
    assert outcome.status == "cancelled"
    assert outcome.exit_code == ExitCode.CANCELLED  # 130; never 0: CI must not see a pass
    assert int(outcome.exit_code) == 130
    view = env.store.get_build(outcome.build)
    assert view.status == "cancelled"
    assert view.action_counts == {"cancelled": 2, "done": 1}
    assert signal.getsignal(signal.SIGINT) is before  # handler restored


# R8: an interrupted driver without its own handler leaves SIGINT alone.
def test_no_sigint_handler_by_default(env: Env) -> None:
    before = signal.getsignal(signal.SIGINT)
    seen: list[object] = []
    executor = env.executor(on_poll=lambda _: seen.append(signal.getsignal(signal.SIGINT)))
    env.run(env.plan({"a": Node()}), executor)
    assert seen
    assert all(h is before for h in seen)


# P2-04 R1, R3: each action's store row says what it is (rule kind, params, toolchain), so
# provenance queries can show it and `ebs tests` can select `*.sim` rules from the store alone.
def test_actions_record_rule_params_toolchain(env: Env) -> None:
    plan = env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))})
    outcome = env.run(plan, env.executor())
    rows = env.store.list_actions(outcome.build)
    assert [(r.action_id, r.rule, r.params, r.toolchain) for r in rows] == [
        ("a", "shell", {}, None),
        ("b", "shell", {}, None),
    ]
    tc = hash_bytes(b"toolchain")
    spec = dataclasses.replace(
        plan.actions[0],
        rule=RuleRef("questa.sim", "1"),
        params={"test": "smoke", "seed": 3, "gui": False, "plusargs": ("+a", "+b")},
        toolchain=ToolchainRef("questa", "questa/2025.2", tc),
    )
    row = action_row(spec)
    assert (row.rule, row.toolchain) == ("questa.sim", tc)
    assert row.params == {"test": "smoke", "seed": 3, "gui": False, "plusargs": ["+a", "+b"]}
