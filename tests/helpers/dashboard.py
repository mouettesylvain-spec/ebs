"""Dashboard reads (P2-06) as a contract both stores pass: `list_builds` and `domain_stats`, run
in memory (tests/unit/meta/test_dashboard_store.py) and on PostgreSQL
(tests/integration/meta/test_dashboard_pg.py)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.meta.api import (
    ActionRow,
    BuildCreate,
    BuildFilter,
    BuildId,
    DomainStats,
    Event,
    EventType,
    PendingWait,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
    StepStats,
)
from ebs.meta.service.deps import ServiceBackend

DOMAIN = "dash"


def new_build(
    store: ServiceBackend, *, user: str = "alice", project: str = "cpu", domain: str = DOMAIN
) -> BuildId:
    return store.create_build(
        BuildCreate(
            uuid=uuid4(), domain=domain, project=project, plan_digest=hash_bytes(b"plan"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name=user, cache_mode="write",
        )
    )  # fmt: skip


def result(key_seed: str, *, cpu_s: int, wall_s: int, passed: bool = True) -> ResultManifest:
    return ResultManifest(
        action_key=hash_bytes(key_seed.encode()), status="passed" if passed else "failed",
        exit_code=0 if passed else 1, inputs={}, outputs={}, log=None, summary={},
        resources=ResourceUsage(max_rss_kb=1, cpu_s=cpu_s, wall_s=wall_s),
        runner=RunnerInfo(version="0", host="node-a"),
    )  # fmt: skip


def run_action(
    store: ServiceBackend,
    build: BuildId,
    action_id: str,
    *,
    cached: bool = False,
    cpu_s: int = 0,
    wall_s: int = 0,
    passed: bool = True,
) -> None:
    """Add `action_id` and take it to cached, done or failed with a result of that usage."""
    store.add_actions(build, [ActionRow(action_id=action_id, step=action_id.split("[", 1)[0])])
    manifest = result(f"{build}:{action_id}", cpu_s=cpu_s, wall_s=wall_s, passed=passed)
    store.set_action_state(
        build, action_id, "cached" if cached else "running", key=manifest.action_key
    )
    store.record_result(build, action_id, manifest)
    if not cached:
        store.set_action_state(build, action_id, "done" if passed else "failed")


def check_list_builds(store: ServiceBackend, clock: FakeClock) -> None:
    t0 = clock.now()
    b1 = new_build(store, user="alice", project="cpu")
    clock.advance(3600)
    b2 = new_build(store, user="bob", project="cpu")
    run_action(store, b2, "lint")
    store.finish_build(b2, "passed")
    clock.advance(3600)
    b3 = new_build(store, user="alice", project="gpu")
    store.finish_build(b3, "failed")
    other = new_build(store, domain="dash-other")

    def ids(**kw: object) -> list[int]:
        return [b.id for b in store.list_builds(DOMAIN, BuildFilter(**kw))]  # type: ignore[arg-type]

    assert ids() == [b3, b2, b1]  # newest first, never another domain's
    assert other not in ids()
    assert ids(user="alice") == [b3, b1]
    assert ids(status="running") == [b1]
    assert ids(project="cpu") == [b2, b1]
    assert ids(user="alice", project="cpu") == [b1]
    assert ids(since=t0 + timedelta(minutes=30)) == [b3, b2]
    assert ids(until=t0 + timedelta(hours=1)) == [b1]  # until is exclusive
    assert ids(since=t0 + timedelta(hours=1), until=t0 + timedelta(hours=2)) == [b2]
    view = store.list_builds(DOMAIN, BuildFilter(user="bob"))[0]
    assert view == store.get_build(b2)  # with its action counts
    assert view.action_counts == {"done": 1}
    with pytest.raises(MetadataError):
        store.list_builds("not a domain!", BuildFilter())


def check_domain_stats(store: ServiceBackend, clock: FakeClock) -> None:
    start = clock.now()
    before = new_build(store)  # outside the window
    run_action(store, before, "compile", cached=True, cpu_s=999, wall_s=999)
    clock.advance(60)
    since = clock.now()
    b = new_build(store)
    run_action(store, b, "compile", cached=True, cpu_s=100, wall_s=120)
    run_action(store, b, "sim[1]", cpu_s=30, wall_s=40)
    run_action(store, b, "sim[2]", cached=True, cpu_s=7, wall_s=8)
    run_action(store, b, "sim[3]", cpu_s=5, wall_s=5, passed=False)  # a test failure ran too
    store.add_actions(b, [ActionRow(action_id="lint", step="lint")])  # never ran: not counted
    # Another domain's build in the window counts nowhere (stats are scoped by domain).
    x = new_build(store, domain="dash-other")
    run_action(store, x, "compile", cached=True, cpu_s=5000, wall_s=5000)
    store.emit(
        Event(ts=since, build=x, type="pending", action_id="compile", data={"reason": "resources"})
    )

    def emit(action: str, type_: EventType, at: int, **data: object) -> None:
        ts = since + timedelta(seconds=at)
        store.emit(Event(ts=ts, build=b, type=type_, action_id=action, data=data))

    # sim[1]: 60 s waiting for two features at once; then it runs.
    emit("sim[1]", "submitted", 0, job_id="1", attempt=1, batch_size=1)
    emit("sim[1]", "pending", 0, reason="licenses", licenses=["simfeat", "addon"])
    emit("sim[1]", "pending", 30, reason="licenses", licenses=["simfeat", "addon"])  # same wait
    emit("sim[1]", "running", 60, job_id="1")
    # sim[3]: 10 s for resources, then 20 s for licenses it does not name, then runs.
    emit("sim[3]", "pending", 100, reason="resources")
    emit("sim[3]", "pending", 110, reason="licenses")
    emit("sim[3]", "running", 130, job_id="3")
    # lint: still pending for licenses; counted up to the store's clock (500 s later).
    emit("lint", "pending", 200, reason="licenses", licenses=["lintfeat"])
    # Events are free-form: an unknown reason counts as "other" and never breaks the stats.
    emit("compile", "pending", 300, reason="bogus", licenses="not a list")
    emit("compile", "running", 310, job_id="9")
    clock.advance(700)
    until = since + timedelta(days=1)  # later than now: the open wait ends at now
    stats = store.domain_stats(DOMAIN, since=since, until=until)

    assert stats == DomainStats(
        domain=DOMAIN, since=since, until=until, builds=1,
        steps=[
            StepStats(step="compile", cached=1, ran=0, cached_cpu_s=100, cached_wall_s=120,
                      ran_cpu_s=0),
            StepStats(step="lint", cached=0, ran=0, cached_cpu_s=0, cached_wall_s=0, ran_cpu_s=0),
            StepStats(step="sim", cached=1, ran=2, cached_cpu_s=7, cached_wall_s=8, ran_cpu_s=35),
        ],
        waits=[
            PendingWait(reason="licenses", feature="", waits=1, wait_s=20),
            PendingWait(reason="licenses", feature="addon", waits=1, wait_s=60),
            PendingWait(reason="licenses", feature="lintfeat", waits=1, wait_s=500),
            PendingWait(reason="licenses", feature="simfeat", waits=1, wait_s=60),
            PendingWait(reason="other", feature=None, waits=1, wait_s=10),
            PendingWait(reason="resources", feature=None, waits=1, wait_s=10),
        ],
    )  # fmt: skip
    early = store.domain_stats(DOMAIN, since=start, until=since)
    assert early.builds == 1
    assert early.steps == [
        StepStats(step="compile", cached=1, ran=0, cached_cpu_s=999, cached_wall_s=999, ran_cpu_s=0)
    ]
    assert early.waits == []
    # An earlier `until` cuts the open wait there.
    cut = store.domain_stats(DOMAIN, since=since, until=since + timedelta(seconds=250))
    assert [w.wait_s for w in cut.waits if w.feature == "lintfeat"] == [50]
    for bad in [(since, since), (until, since), (since, since + timedelta(days=400))]:
        with pytest.raises(MetadataError):
            store.domain_stats(DOMAIN, since=bad[0], until=bad[1])


def check_result_summaries(store: ServiceBackend) -> None:
    # Another build (of another domain) with the same action ids never shows in b's results.
    o = new_build(store, domain="dash-other")
    run_action(store, o, "sim[t1]", passed=False)
    run_action(store, o, "lint")
    b = new_build(store)
    run_action(store, b, "sim[t2]", passed=False)
    run_action(store, b, "sim[t1]", cached=True)
    run_action(store, b, "lint")
    store.add_actions(b, [ActionRow(action_id="sim[t3]", step="sim")])  # no result yet
    key = hash_bytes(b"with summary")
    store.add_actions(b, [ActionRow(action_id="sim[t4]", step="sim")])
    store.set_action_state(b, "sim[t4]", "running", key=key)
    summary: dict[str, str | int] = {"test": "t4", "seed": 7, "signature": "UVM_ERROR <n>"}
    manifest = result("x", cpu_s=1, wall_s=1, passed=False).model_copy(
        update={"action_key": key, "log": hash_bytes(b"log"), "summary": summary}
    )
    store.record_result(b, "sim[t4]", manifest)
    found = store.result_summaries(b, step="sim")
    assert [(r.action_id, r.status) for r in found] == [
        ("sim[t1]", "passed"),
        ("sim[t2]", "failed"),
        ("sim[t4]", "failed"),
    ]
    last = found[-1]
    assert (last.exit_code, last.log, last.summary) == (1, hash_bytes(b"log"), summary)
    every = [r.action_id for r in store.result_summaries(b)]
    assert every == ["lint", "sim[t1]", "sim[t2]", "sim[t4]"]
    with pytest.raises(MetadataError):
        store.result_summaries(BuildId(999_999))
