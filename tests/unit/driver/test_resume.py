"""Resuming a build whose driver died, and the one-driver-per-build lease (P2-08 R2, R3).

A dead driver is simulated by `start_build` plus store writes that leave the build as a driver
would have left it mid-run; the jobs it submitted live on in the same `ScriptedExecutor` (they
outlive the driver, as SLURM jobs do), which records job ids at submission like SLURM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from ebs.core.errors import DriverBusyError, MetadataError
from ebs.driver.build import StartedBuild, events_path, start_build
from ebs.driver.events import EventLog, read_events
from ebs.driver.resume import resume_build
from ebs.driver.scheduler import DriverConfig
from ebs.exec.api import Executor, SubmitBatch
from ebs.meta.api import DRIVER_LEASE_TTL_S, ActionState, BuildId, DriverLease
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.planner import refine_plan
from ebs.plan.types import Plan
from tests.helpers.driver import DOMAIN, Env, Node, ScriptedExecutor, result_for

pytestmark = pytest.mark.unit

CHAIN = {"a": Node(), "b": Node(deps=(("a", "out"),))}


def started(env: Env, plan: Plan, *, cache_mode: str = "write") -> StartedBuild:
    return start_build(
        plan,
        cas=env.cas,
        store=env.store,
        workdir=env.workdir,
        user="alice",
        cache_mode=cache_mode,  # type: ignore[arg-type]
        clock=env.clock,
    )


def resume(env: Env, build: BuildId, ex: Executor, **kwargs: Any) -> Any:
    kwargs.setdefault("owner", "node-b:2:resumer")
    return resume_build(
        build,
        cas=env.cas,
        store=env.store,
        executor=ex,
        workdir=env.workdir,
        clock=env.clock,
        **kwargs,
    )


@dataclass(frozen=True)
class LeftBehind:
    """How the dead driver left action `a`, and what resuming must do."""

    state: ActionState
    with_job: bool  # a job id was recorded (the executor can re-attach to it)
    final_event: bool  # the driver had written `a`'s `finished` event
    submissions: int  # `a` jobs in total, the dead driver's included
    a_final: str
    b_final: str
    status: str


CASES = {
    "queued": LeftBehind("queued", False, False, 1, "done", "done", "passed"),
    "pending-with-job": LeftBehind("pending", True, False, 1, "done", "done", "passed"),
    "running-with-job": LeftBehind("running", True, False, 1, "done", "done", "passed"),
    "running-job-unknown": LeftBehind("running", False, False, 1, "done", "done", "passed"),
    "done": LeftBehind("done", True, True, 1, "done", "done", "passed"),
    "failed": LeftBehind("failed", True, True, 1, "failed", "skipped", "failed"),
    "cached": LeftBehind("cached", False, True, 0, "cached", "done", "passed"),
    "infra-awaiting-retry": LeftBehind("infra_failed", True, False, 2, "done", "done", "passed"),
    "infra-final": LeftBehind(
        "infra_failed", True, True, 1, "infra_failed", "skipped", "infra_failed"
    ),
}


def leave(env: Env, ex: ScriptedExecutor, s: StartedBuild, plan: Plan, case: LeftBehind) -> None:
    """Put action `a` of build `s` where a driver killed at that point would have left it."""
    store, build = env.store, s.build
    spec = plan.action("a")
    events = EventLog(store, build, events_path(env.workdir, s.uuid), env.clock)
    if case.state == "queued":
        return
    if case.state == "cached":
        hit = result_for(spec)
        store.record_result(build, "a", hit)
        store.set_action_state(build, "a", "cached", key=spec.key, result_key=hit.action_key)
        events.emit("cache_hit", "a", key=str(spec.key), status="passed")
        return
    if case.with_job:
        batch = SubmitBatch(plan=s.plan_digest, domain=DOMAIN, build=build, actions=(spec,))
        ex.submit(batch)  # records `pending` + its job id, like the SLURM executor
    else:
        store.set_action_state(build, "a", "pending", key=spec.key)
    if case.state == "pending":
        return
    store.set_action_state(build, "a", "running", key=spec.key)
    if case.state in ("done", "failed"):
        for _ in range(3):  # the job ends (and posts) while no driver watches
            ex.poll([h for h in ex._jobs if h.action_id == "a"])
        result = store.get_result(build, "a")
        assert result is not None
        store.set_action_state(build, "a", case.state, result_key=result.action_key)
        events.emit("finished", "a", state=case.state, exit_code=result.exit_code)
    elif case.state == "infra_failed":
        store.set_action_state(build, "a", "infra_failed", infra_reason="node_fail")
        if case.final_event:
            events.emit("finished", "a", state="infra_failed")


# R2
@pytest.mark.parametrize("case", list(CASES.values()), ids=list(CASES))
def test_reconcile_states(env: Env, case: LeftBehind) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    if case.state == "failed":
        ex.script("a", "fail")
    s = started(env, plan)
    leave(env, ex, s, plan, case)

    outcome = resume(env, s.build, ex, config=DriverConfig(poll_min_s=1.0, poll_max_s=1.0))

    assert ex.runs("a") == case.submissions
    assert outcome.states == {"a": case.a_final, "b": case.b_final}
    assert outcome.status == case.status
    assert outcome.build == s.build
    assert outcome.uuid == s.uuid
    assert env.store.get_build(s.build).status == case.status
    if case.with_job and case.state in ("pending", "running"):
        assert ex.adopted == ["a"]  # re-attached, not resubmitted
    else:
        assert ex.adopted == []
    if case.b_final == "done":
        b = ex.submitted_specs[-1]
        assert b.action_id == "b"
        assert ex.runs("b") == 1
        produced = env.store.get_result(s.build, "a")
        assert produced is not None
        assert {r.id for r in b.inputs} >= {produced.outputs["out"].id}  # refined from a's result
    events = [(e.type, e.action_id) for e in read_events(events_path(env.workdir, s.uuid))]
    assert ("build_resumed", None) in events
    assert events[-1] == ("build_finished", None)


# R2: a finished build cannot be resumed; the error says what to do.
def test_resume_finished_build_refused(env: Env) -> None:
    plan = env.plan({"a": Node()})
    s = started(env, plan)
    env.store.finish_build(s.build, "cancelled")
    with pytest.raises(DriverBusyError, match="finished"):
        resume(env, s.build, env.executor())


# R3: a build has one live driver: a second one is refused while the first heartbeats, and may
# take over once its lease has expired.
def test_single_driver_lease(env: Env) -> None:
    plan = env.plan({"a": Node()})
    s = started(env, plan)
    lease = env.store.claim_driver(s.build, "node-a:1:first", DRIVER_LEASE_TTL_S)
    assert lease.owner == "node-a:1:first"
    ex = env.executor(record_jobs=True)
    with pytest.raises(DriverBusyError, match="node-a:1:first") as err:
        resume(env, s.build, ex)
    assert "expires" in str(err.value)
    assert ex.submitted == []
    assert env.store.get_build(s.build).status == "running"  # refusing never finishes it
    env.clock.advance(DRIVER_LEASE_TTL_S + 1)
    outcome = resume(env, s.build, ex)
    assert outcome.status == "passed"
    assert ex.runs("a") == 1


# R3: `ebs build` holds the lease while it runs, so a resume meanwhile is refused.
def test_running_build_holds_lease(env: Env) -> None:
    plan = env.plan({"a": Node()})
    refused: list[str] = []

    def second_driver(polls: int) -> None:
        if polls == 1:
            try:  # the build of this test is the store's first: id 1
                resume(env, BuildId(1), env.executor())
            except DriverBusyError as exc:
                refused.append(str(exc))

    ex = env.executor(on_poll=second_driver)
    ex.script("a", "pass")
    outcome = env.run(plan, ex)
    assert outcome.status == "passed"
    assert len(refused) == 1
    assert "driver" in refused[0]


# R3: a driver whose lease was taken over stops at its next heartbeat, leaving the jobs (now the
# other driver's) running and the build unfinished.
def test_lost_lease_stops_driver_without_cancelling(env: Env) -> None:
    plan = env.plan({"a": Node()})

    def takeover(polls: int) -> None:
        if polls == 1:
            env.clock.advance(DRIVER_LEASE_TTL_S + 1)  # the first driver stalled
            assert env.store.claim_driver(BuildId(1), "node-c:3:new", 60).owner == "node-c:3:new"

    ex = env.executor(on_poll=takeover)
    ex.script("a", "hang")
    with pytest.raises(DriverBusyError, match="node-c:3:new"):
        env.run(plan, ex)
    assert ex.cancelled == []
    assert env.store.get_build(BuildId(1)).status == "running"


# R3: a live driver keeps its lease well past one TTL by renewing it on its heartbeat.
def test_live_driver_renews_lease(env: Env) -> None:
    plan = env.plan({"a": Node()})
    refused: list[str] = []

    def time_passes(polls: int) -> None:
        env.clock.advance(DRIVER_LEASE_TTL_S / 2)
        if polls == 6:  # 3 TTLs after the claim
            try:
                resume(env, BuildId(1), env.executor())
            except DriverBusyError as exc:
                refused.append(str(exc))

    ex = env.executor(on_poll=time_passes)
    ex.script("a", "pass", running_polls=8)
    outcome = env.run(plan, ex, poll_min_s=1.0, poll_max_s=1.0)
    assert outcome.status == "passed"
    assert len(refused) == 1


class FlakyClaims(InMemoryMetadataStore):
    """The second `claim_driver` (the first renewal) fails, as during a metadata outage."""

    claims = 0

    def claim_driver(self, build: BuildId, owner: str, ttl_s: int) -> DriverLease:
        self.claims += 1
        if self.claims == 2:
            raise MetadataError("injected: metadata service unreachable")
        return super().claim_driver(build, owner, ttl_s)


# R3: a renewal the store cannot answer is logged, not fatal: the build goes on.
def test_lease_renewal_error_tolerated(env: Env) -> None:
    env.store = FlakyClaims(clock=env.clock)
    plan = env.plan({"a": Node()})
    ex = env.executor(on_poll=lambda polls: env.clock.advance(DRIVER_LEASE_TTL_S / 2))
    ex.script("a", "pass", running_polls=4)
    outcome = env.run(plan, ex, poll_min_s=1.0, poll_max_s=1.0)
    assert outcome.status == "passed"
    assert ex.cancelled == []
    assert env.store.claims >= 3  # it renewed again after the failure


# R2: outputs restored from the dead driver's results are held by the new driver's GC lease
# (its consumers still need them; the old lease expires with the old driver).
def test_restored_outputs_leased(env: Env) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    leave(env, ex, s, plan, CASES["done"])
    produced = env.store.get_result(s.build, "a")
    assert produced is not None
    seen: list[bool] = []

    def check_lease(polls: int) -> None:
        if not seen:  # the resumed driver's first poll, while b runs
            leased = env.store.gc_roots(DOMAIN, env.clock.now()).leased
            seen.append(produced.outputs["out"].digest in leased)

    ex._on_poll = check_lease
    assert resume(env, s.build, ex).status == "passed"
    assert seen == [True]


class NoAdopt:
    """An executor whose jobs die with the driver (like the local one): it cannot re-attach."""

    def __init__(self, inner: ScriptedExecutor) -> None:
        self.name = inner.name
        self.submit = inner.submit
        self.poll = inner.poll
        self.cancel = inner.cancel


# R2: a build whose SLURM jobs still run cannot be resumed with an executor that cannot
# re-attach to them: they would run untracked while the actions ran again.
def test_resume_refuses_executor_that_cannot_attach(env: Env) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    leave(env, ex, s, plan, CASES["running-with-job"])
    with pytest.raises(DriverBusyError, match="--executor slurm"):
        resume(env, s.build, NoAdopt(ex))
    assert ex.runs("a") == 1
    assert env.store.get_build(s.build).status == "running"  # untouched: resume it properly
    assert resume(env, s.build, ex).status == "passed"


# R2: a still-pending SLURM job counts too (it would start later, while the action reran).
def test_resume_refuses_pending_job_it_cannot_attach(env: Env) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    leave(env, ex, s, plan, CASES["pending-with-job"])
    with pytest.raises(DriverBusyError, match="--executor slurm"):
        resume(env, s.build, NoAdopt(ex))
    assert ex.runs("a") == 1


FAST = DriverConfig(poll_min_s=1.0, poll_max_s=1.0)


# R2: a cached test failure stays a failure: its consumers are skipped, the build fails.
def test_resume_cached_failure(env: Env) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    spec = plan.action("a")
    hit = result_for(spec, passed=False)
    env.store.record_result(s.build, "a", hit)
    env.store.set_action_state(s.build, "a", "cached", key=spec.key, result_key=hit.action_key)
    outcome = resume(env, s.build, ex, config=FAST)
    assert outcome.states == {"a": "cached", "b": "skipped"}
    assert outcome.status == "failed"
    assert ex.runs("b") == 0


# R2: an action skipped because its inputs were never produced still fails the build.
def test_resume_skipped_missing_input_fails(env: Env) -> None:
    plan = env.plan(CHAIN)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    leave(env, ex, s, plan, CASES["done"])
    env.store.set_action_state(s.build, "b", "skipped")
    EventLog(env.store, s.build, events_path(env.workdir, s.uuid), env.clock).emit(
        "finished", "b", state="skipped", reason="inputs not produced: a.out"
    )
    outcome = resume(env, s.build, ex, config=FAST)
    assert outcome.states == {"a": "done", "b": "skipped"}
    assert outcome.status == "failed"


# R2: the build's own cache mode applies, whatever the resuming driver was given: a
# `--no-cache` build never reads the cache after a resume.
def test_resume_uses_build_cache_mode(env: Env) -> None:
    plan = env.plan({"a": Node()})
    ex = env.executor(record_jobs=True)
    s = started(env, plan, cache_mode="off")
    spec = plan.action("a")
    assert spec.key is not None
    env.store.cache_put(DOMAIN, spec.key, result_for(spec))
    config = DriverConfig(cache_mode="read", poll_min_s=1.0, poll_max_s=1.0)
    outcome = resume(env, s.build, ex, config=config)
    assert outcome.states == {"a": "done"}
    assert ex.runs("a") == 1


# R2: restored successes refine the plan in order, through a chain (a done, b done, c runs).
def test_resume_refines_through_chain(env: Env) -> None:
    nodes = {"a": Node(), "b": Node(deps=(("a", "out"),)), "c": Node(deps=(("b", "out"),))}
    plan = env.plan(nodes)
    ex = env.executor(record_jobs=True)
    s = started(env, plan)
    leave(env, ex, s, plan, CASES["done"])
    ra = env.store.get_result(s.build, "a")
    assert ra is not None
    b = refine_plan(plan, {("a", "out"): ra.outputs["out"].id}).action("b")
    rb = result_for(b)
    env.store.record_result(s.build, "b", rb)
    for state in ("pending", "running"):
        env.store.set_action_state(s.build, "b", state, key=b.key)
    env.store.set_action_state(s.build, "b", "done", result_key=rb.action_key)
    EventLog(env.store, s.build, events_path(env.workdir, s.uuid), env.clock).emit(
        "finished", "b", state="done", exit_code=0
    )
    outcome = resume(env, s.build, ex, config=FAST)
    assert outcome.states == {"a": "done", "b": "done", "c": "done"}
    c = ex.submitted_specs[-1]
    assert c.action_id == "c"
    assert {r.id for r in c.inputs} >= {rb.outputs["out"].id}
