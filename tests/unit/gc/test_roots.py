"""GC roots (P1-09 R2): releases, pinned builds and the leases of running builds."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.core.digest import hash_bytes
from ebs.core.errors import MetadataError
from ebs.gc.leases import LeaseKeeper
from ebs.gc.roots import root_set
from ebs.gc.run import collect
from ebs.meta.api import LEASE_TTL_S, BuildId, GcRoots, OutputResult
from ebs.release.releases import create_release
from tests.helpers.driver import Node, make_env
from tests.helpers.gc import DOMAIN, World, present
from tests.helpers.release import Action, finish


def roots(world: World) -> GcRoots:
    return world.store.gc_roots(DOMAIN, world.clock.now())


# R2
def test_pins_leases_releases(world: World) -> None:
    out, log = world.blob(b"out"), world.blob(b"log")
    plan = world.blob(b"plan bytes")
    pinned = world.build(plan=plan)
    world.record(pinned, "lint", {"report": out}, log=log)
    world.store.finish_build(pinned, "passed")

    # Pinned builds: plan, outputs and logs.
    assert roots(world).pinned == frozenset()
    world.store.pin_build(pinned, by="alice", reason="sign-off")
    world.store.pin_build(pinned, by="bob")  # idempotent
    assert world.store.get_build(pinned).pinned is True
    assert roots(world).pinned == {plan, out, log}

    # Live leases of running builds; they expire after their TTL and go with the build.
    running = world.build()
    leased = world.blob(b"input")
    lease = world.store.acquire_lease(DOMAIN, running, [leased], ttl_s=LEASE_TTL_S)
    assert roots(world).leased == {leased}
    world.advance(hours=0.2)
    extra = hash_bytes(b"produced later")
    world.store.renew_lease(running, lease, LEASE_TTL_S, add=[extra])
    world.advance(hours=0.2)  # past the first expiry, within the renewed one
    assert roots(world).leased == {leased, extra}
    world.advance(hours=0.3)
    assert roots(world).leased == frozenset()
    with pytest.raises(MetadataError, match="no live lease"):
        world.store.renew_lease(running, lease, LEASE_TTL_S)
    second = world.store.acquire_lease(DOMAIN, running, [leased], ttl_s=LEASE_TTL_S)
    with pytest.raises(MetadataError, match="no live lease"):
        world.store.renew_lease(pinned, second, LEASE_TTL_S)  # another build's lease
    world.store.finish_build(running, "passed")
    assert roots(world).leased == frozenset()
    with pytest.raises(MetadataError, match="finished"):
        world.store.acquire_lease(DOMAIN, running, [leased], ttl_s=LEASE_TTL_S)
    other = world.build()
    with pytest.raises(MetadataError, match="domain"):
        world.store.acquire_lease("elsewhere", other, [leased], ttl_s=LEASE_TTL_S)
    with pytest.raises(MetadataError, match="ttl_s"):
        world.store.acquire_lease(DOMAIN, other, [leased], ttl_s=0)

    # No release yet (test_release_roots covers them).
    assert roots(world).released == frozenset()
    assert root_set(roots(world)) == {plan, out, log}

    # Unpinning removes the root.
    assert world.store.unpin_build(pinned) is True
    assert world.store.unpin_build(pinned) is False
    assert world.store.get_build(pinned).pinned is False
    assert roots(world).pinned == frozenset()


# R2: roots survive the sweep however old they are; an expired lease protects nothing.
def test_roots_are_never_collected(world: World) -> None:
    tree = world.tree({"a.v": b"a", "sub/b.v": b"b"})
    out, plan = world.blob(b"pinned output"), world.blob(b"plan")
    build = world.build(plan=plan)
    world.record(build, "gen", {"model": tree, "out": out})
    world.store.finish_build(build, "passed")
    world.store.pin_build(build, by="alice")
    running = world.build()
    leased, expiring = world.blob(b"leased"), world.blob(b"expiring")
    world.store.acquire_lease(DOMAIN, running, [expiring], ttl_s=60)
    keep = present(world.cas) - {expiring}
    for _ in range(4):  # 40 days: past the TTL and the grace period
        world.advance(days=10)
        # A long build keeps its lease alive with a fresh one per heartbeat.
        world.store.acquire_lease(DOMAIN, running, [leased], ttl_s=LEASE_TTL_S)
        collect(world.store, world.cas, clock=world.clock)
    assert present(world.cas) == keep
    assert expiring not in present(world.cas)


# R2: the driver leases the plan and its inputs at build start, renews the lease on its
# heartbeat, adds what the build produces, and drops it when the build finishes.
def test_driver_holds_a_lease(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    plan = env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))})
    source_ids = {r.id for s in plan.actions for r in s.inputs if r.id is not None}
    seen: list[GcRoots] = []

    def on_poll(n: int) -> None:
        env.clock.advance(LEASE_TTL_S / 2)  # a slow build: the lease must be renewed
        seen.append(env.store.gc_roots(DOMAIN, env.clock.now()))

    outcome = env.run(plan, env.executor(on_poll=on_poll), cache_mode="write")
    assert outcome.status == "passed"
    assert seen
    assert all(source_ids <= r.leased for r in seen)
    produced = env.store.get_result(outcome.build, "a")
    assert produced is not None
    assert produced.outputs["out"].digest in seen[-1].leased
    assert env.store.get_build(outcome.build).plan_digest in seen[0].leased
    assert env.store.gc_roots(DOMAIN, env.clock.now()).leased == frozenset()


# R2: LeaseKeeper renews every ttl/3, flushes added digests on the next heartbeat, and acquires
# a fresh lease (with everything it held) if the old one is gone.
def test_lease_keeper(world: World) -> None:
    build = world.build()
    a, b = hash_bytes(b"a"), hash_bytes(b"b")
    keeper = LeaseKeeper(world.store, DOMAIN, build, world.clock, ttl_s=900)
    keeper.heartbeat()  # not started: nothing to do
    keeper.start([a])
    assert roots(world).leased == {a}
    keeper.add([b])
    keeper.heartbeat()
    assert roots(world).leased == {a, b}
    world.advance(hours=14 / 60)  # 14 min: renewed after 5 and 10 minutes only if heartbeats ran
    keeper.heartbeat()
    world.advance(hours=10 / 60)
    assert roots(world).leased == {a, b}
    world.advance(hours=1)  # missed heartbeats: the lease expired
    world.store.gc_expire_leases(DOMAIN, world.clock.now())
    keeper.heartbeat()
    assert roots(world).leased == {a, b}
    world.store.finish_build(BuildId(build), "passed")
    world.advance(hours=0.1)
    keeper.heartbeat()  # finished build: the lease is gone and cannot be renewed; logged only
    assert roots(world).leased == frozenset()


# P2-02 R2 (I19): released outputs are GC roots, also once yanked; a tree root keeps all of it.
def test_release_roots(world: World) -> None:
    tree = world.tree({"a.v": b"a", "sub/b.v": b"b"}, register="all")
    report = world.blob(b"lint report")
    other = world.blob(b"not released")
    outputs = {
        "worklib": OutputResult(digest=tree, id=hash_bytes(b"nd"), type="tree", size=0),
        "report": OutputResult(digest=report, id=report, type="file", size=11),
        "other": OutputResult(digest=other, id=other, type="file", size=12),
    }
    build = finish(world.store, {"gen": Action(outputs)})
    rel = create_release(
        world.store, name="rtl-team/cpu", version="1.0", build=build,
        outputs=["gen.worklib", "gen.report"], by="alice",
    )  # fmt: skip
    assert roots(world).released == {tree, report}
    assert world.store.gc_roots("elsewhere", world.clock.now()).released == frozenset()
    world.store.yank_release(rel.id, by="alice", reason="superseded")
    assert roots(world).released == {tree, report}  # a yanked release is still a release
    kept = present(world.cas) - {other}
    for _ in range(4):  # 40 days: past the TTL and the grace period
        world.advance(days=10)
        collect(world.store, world.cas, clock=world.clock)
    assert present(world.cas) == kept
