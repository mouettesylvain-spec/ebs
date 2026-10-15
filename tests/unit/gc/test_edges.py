"""Edge cases of GC v1 found by mutation testing (P1-09 R2-R5, I15)."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from datetime import timedelta

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import MetadataError
from ebs.gc.leases import LeaseKeeper
from ebs.gc.mark import mark
from ebs.gc.roots import TreeGraph
from ebs.gc.run import collect, take_snapshot
from ebs.gc.sweep import GcConfig, plan_sweep
from ebs.meta.api import BuildId, DomainPolicy, GcCacheEntry, GcRoots, LeaseId, Tombstone
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.gc import DOMAIN, World, manifest, present


def tombstones(world: World) -> dict[object, Tombstone]:
    return {t.digest: t for t in world.store.gc_tombstones(DOMAIN)}


# R2/R5 (I15): a lease is a root even when its objects were last touched long ago (a build
# running for days renews without re-adding them): never evicted, never tombstoned.
def test_quota_spares_long_held_lease(world: World) -> None:
    held = world.blob(b"h" * 50)
    build = world.build()
    lease = world.store.acquire_lease(DOMAIN, build, [held], 86_400)
    for _ in range(4):
        world.advance(hours=12)
        world.store.renew_lease(build, lease, 86_400)
    world.store.set_policy(DOMAIN, DomainPolicy(quota_bytes=50, high_water=0.5, low_water=0.1))
    report = collect(world.store, world.cas, clock=world.clock)
    assert report.quota is not None
    assert report.quota.evicted == []
    assert held not in tombstones(world)
    world.advance(days=31)
    world.store.acquire_lease(DOMAIN, build, [held], 86_400)  # still running, after the TTL
    collect(world.store, world.cas, clock=world.clock)
    assert held not in tombstones(world)


# R2 (I15, I19): release outputs are roots, with everything below them.
def test_released_tree_is_kept(world: World) -> None:
    tree = world.tree({"top.v": b"t", "sub/x.v": b"x"})
    world.advance(days=40)
    snap = take_snapshot(world.store, world.cas, world.clock)
    released = GcRoots(pinned=frozenset(), leased=frozenset(), released=frozenset({tree}))
    plan = plan_sweep(dataclasses.replace(snap, roots=released), GcConfig())
    closure = snap.graph.closure([tree])
    assert closure <= plan.keep
    assert not closure & set(plan.tombstone)
    without = plan_sweep(snap, GcConfig())
    assert closure <= set(without.tombstone)  # the release is what keeps them


# R4 (I15): a child due before the tree containing it stays while the tree stays.
def test_child_due_before_its_tree(world: World) -> None:
    child = world.blob(b"module shared;")
    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)
    assert child in tombstones(world)
    tree = world.tree({"shared.v": b"module shared;", "own.v": b"own"})  # root row only
    world.advance(days=31)  # no GC meanwhile: the child is due, the tree only gets tombstoned
    collect(world.store, world.cas, clock=world.clock)
    assert tree in tombstones(world)
    assert child in present(world.cas)
    world.cas.materialize(tree, "tree", world.tmp / "out")


# R4: a tombstone touched afterwards but unkept again is re-tombstoned: the grace restarts.
def test_touched_tombstone_restarts(world: World) -> None:
    x = world.blob(b"x")
    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)
    world.advance(hours=2)
    world.store.touch(DOMAIN, [x])
    world.advance(days=32)
    collect(world.store, world.cas, clock=world.clock)  # drops the stale tombstone
    collect(world.store, world.cas, clock=world.clock)  # tombstones it afresh
    assert tombstones(world)[x].deleted_rows_at == world.clock.now()
    assert x in present(world.cas)
    world.advance(hours=25)
    collect(world.store, world.cas, clock=world.clock)
    assert x not in present(world.cas)


# R4/R6: a dry run reports as deleted only what the real run deletes.
@pytest.mark.parametrize("case", ["not_due", "touched", "referenced", "touched_at_tombstone"])
def test_dry_run_counts_only_deletable(world: World, case: str) -> None:
    x = world.blob(b"tombstoned object")
    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)
    if case == "not_due":
        world.advance(hours=23)
    elif case == "touched":
        world.advance(hours=2)
        world.store.touch(DOMAIN, [x])
        world.advance(hours=23)
    elif case == "referenced":
        world.advance(hours=25)
        key = hash_bytes(b"new row")
        world.store.cache_put(DOMAIN, key, manifest(key, {"out": x}, cas=world.cas))
    else:  # touched in the very instant the tombstone was written: counts as a use
        stone = tombstones(world)[x]
        world.store._blobs[(DOMAIN, str(x))].last_access = stone.deleted_rows_at
        world.advance(hours=25)
    dry = collect(world.store, world.cas, clock=world.clock, dry_run=True)
    assert dry.deleted == 0
    assert collect(world.store, world.cas, clock=world.clock).deleted == 0
    assert x in present(world.cas)


# R3: marking repeats until stable, whatever the order of the entries.
def test_mark_fixpoint_is_order_independent() -> None:
    o1, log_a, log_b = (hash_bytes(s) for s in (b"o1", b"log-a", b"log-b"))
    t0 = FakeClock().now()
    a = GcCacheEntry(hash_bytes(b"A"), t0, (o1,), log_a)
    b = GcCacheEntry(hash_bytes(b"B"), t0, (log_a,), log_b)  # needs A's log
    marked = mark([o1], [b, a], TreeGraph({}))
    assert marked.entries == {a.key, b.key}
    assert {o1, log_a, log_b} <= marked.reachable


# R3/R4: a cache entry whose outputs are rooted keeps its row past the TTL.
def test_rooted_entry_keeps_its_row(world: World) -> None:
    out = world.blob(b"pinned output")
    build = world.build()
    world.record(build, "gen", {"out": out})
    world.store.finish_build(build, "passed")
    world.store.pin_build(build, by="alice")
    key = world.entry("gen", {"out": out})
    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)
    assert key in {e.key for e in world.store.gc_cache_entries(DOMAIN)}


# R4: tombstones are scoped by domain (I13).
def test_drop_tombstones_is_domain_scoped() -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    d, now = hash_bytes(b"d"), store._clock.now()
    for domain in ("a", "b"):
        store.gc_tombstone(domain, [d], now=now, delete_after=now + timedelta(hours=1))
    store.gc_drop_tombstones("a", [d])
    assert store.gc_tombstones("a") == []
    assert [t.digest for t in store.gc_tombstones("b")] == [d]


# R2: GC expires stale leases.
def test_collect_expires_leases(world: World) -> None:
    build = world.build()
    world.store.acquire_lease(DOMAIN, build, [hash_bytes(b"x")], 60)
    world.advance(hours=1)
    collect(world.store, world.cas, clock=world.clock)
    assert world.store._leases == {}


class _FlakyStore(InMemoryMetadataStore):
    def __init__(self, clock: FakeClock, failures: int) -> None:
        super().__init__(clock=clock)
        self.failures = failures

    def acquire_lease(
        self, domain: str, build: BuildId, digests: Iterable[Digest], ttl_s: int
    ) -> LeaseId:
        if self.failures:
            self.failures -= 1
            raise MetadataError("service unavailable")
        return super().acquire_lease(domain, build, digests, ttl_s)


# R2: a lease that could not be acquired is acquired on the next heartbeat; a lease that
# expired is replaced with everything held, including digests added since the last renewal.
def test_lease_keeper_recovers(world: World) -> None:
    store = _FlakyStore(world.clock, failures=1)
    world.store = store
    build = world.build()
    a, c = hash_bytes(b"a"), hash_bytes(b"c")
    keeper = LeaseKeeper(store, DOMAIN, build, world.clock, ttl_s=900)
    keeper.start([a])
    assert store.gc_roots(DOMAIN, world.clock.now()).leased == frozenset()
    keeper.heartbeat()
    assert store.gc_roots(DOMAIN, world.clock.now()).leased == {a}
    keeper.add([c])
    world.advance(hours=1)  # missed heartbeats: the lease expired
    keeper.heartbeat()
    assert store.gc_roots(DOMAIN, world.clock.now()).leased == {a, c}
