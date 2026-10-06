from __future__ import annotations

import os
import signal

import pytest

from ebs.core.errors import ExitCode
from ebs.plan.planfile import plan_digest
from ebs.plan.types import FlowInfo, GitInfo
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
    assert outcome.exit_code == ExitCode.ACTIONS_FAILED  # never 0: CI must not see a pass
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
