"""Sweep (P1-09 R4, I15) and CAS staging cleanup (R8)."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest
from ebs.core.errors import ConfigError
from ebs.gc.run import collect
from ebs.gc.sweep import GcConfig
from ebs.meta.api import DomainPolicy
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.gc import DOMAIN, World, manifest, present


def tombstoned(world: World) -> set[object]:
    return {t.digest for t in world.store.gc_tombstones(DOMAIN)}


def cache_keys(world: World) -> set[object]:
    return {e.key for e in world.store.gc_cache_entries(DOMAIN)}


# R4 (I15): stale cache rows go first and their objects are tombstoned; the bytes go only after
# the grace period, and only if nothing reached or touched them meanwhile.
def test_order_and_grace(world: World) -> None:
    stale_out, stale_log = world.blob(b"stale output"), world.blob(b"stale log")
    stale = world.entry("stale", {"out": stale_out}, log=stale_log)
    touched_out = world.blob(b"used again later")
    touched = world.entry("touched", {"out": touched_out})
    revived_out = world.blob(b"referenced again later")
    revived = world.entry("revived", {"out": revived_out})
    tree = world.tree({"top.v": b"module top;", "rtl/core.v": b"module core;"})
    tree_entry = world.entry("tree", {"model": tree})
    alive_out = world.blob(b"alive output")
    alive = world.entry("alive", {"out": alive_out})
    orphan = world.blob(b"stored but never registered", register=False)
    everything = present(world.cas)

    world.advance(days=31)
    assert world.store.cache_get(DOMAIN, alive) is not None  # a recent hit keeps it
    world.store.touch(DOMAIN, [alive_out])

    first = collect(world.store, world.cas, clock=world.clock)
    assert cache_keys(world) == {alive}  # rows first...
    assert present(world.cas) == everything  # ...bytes later
    gone_later = {stale_out, stale_log, touched_out, revived_out, tree} | (
        everything - {stale_out, stale_log, touched_out, revived_out, alive_out, orphan}
    )
    assert tombstoned(world) == gone_later  # the tree's manifests and blobs too
    assert first.cache_rows_deleted == 4
    assert first.deleted == 0
    assert orphan in {b.digest for b in world.store.gc_blobs(DOMAIN)}  # adopted, kept for a TTL

    world.advance(hours=23)  # within the grace period: nothing is deleted
    assert collect(world.store, world.cas, clock=world.clock).deleted == 0
    assert present(world.cas) == everything

    world.store.touch(DOMAIN, [touched_out])  # a build fetched it
    world.store.cache_put(DOMAIN, revived, manifest(revived, {"out": revived_out}, cas=world.cas))
    world.advance(hours=2)
    third = collect(world.store, world.cas, clock=world.clock)
    assert present(world.cas) == {alive_out, orphan, touched_out, revived_out}
    assert third.deleted == len(gone_later) - 2
    assert tombstoned(world) == set()  # the touched and the reached object were resurrected
    assert stale not in cache_keys(world)
    assert tree_entry not in cache_keys(world)
    assert touched not in cache_keys(world)

    # The orphan is collected one TTL after GC first saw it.
    world.advance(days=31)
    world.store.cache_get(DOMAIN, revived)
    collect(world.store, world.cas, clock=world.clock)
    world.advance(hours=25)
    collect(world.store, world.cas, clock=world.clock)
    assert orphan not in present(world.cas)


# R4: a blob tombstoned with an old tree and then reused by a new tree is not deleted: the new
# tree's use counts for everything below it.
def test_reused_child_survives(world: World) -> None:
    shared = {"lib.v": b"module lib;"}
    old_tree = world.tree({**shared, "a.v": b"a"}, register="all")
    world.advance(days=31)
    collect(world.store, world.cas, clock=world.clock)  # old_tree and lib.v are tombstoned
    new_tree = world.tree({**shared, "b.v": b"b"}, register="root")  # reuses lib.v, untouched
    world.advance(hours=25)
    collect(world.store, world.cas, clock=world.clock)
    assert old_tree not in present(world.cas)
    assert new_tree in present(world.cas)
    world.cas.materialize(new_tree, "tree", world.tmp / "out")  # complete: lib.v is still there
    assert (world.tmp / "out" / "lib.v").read_bytes() == b"module lib;"


# R4: the grace period must outlast the touch throttle, or a touch could go unrecorded.
def test_config_validation() -> None:
    assert GcConfig.from_mapping({}).grace == timedelta(hours=24)
    assert GcConfig.from_mapping({"grace_hours": 2, "tmp_max_age_hours": 6}).tmp_max_age == (
        timedelta(hours=6)
    )
    with pytest.raises(ConfigError, match="grace_hours"):
        GcConfig.from_mapping({"grace_hours": 0})
    with pytest.raises(ConfigError, match="unknown"):
        GcConfig.from_mapping({"grace": 1})
    with pytest.raises(ConfigError, match="tmp_max_age_hours"):
        GcConfig.from_mapping({"tmp_max_age_hours": "1"})


# R8: abandoned uploads in the CAS staging area older than 24 h are removed.
def test_tmp_cleanup(world: World) -> None:
    world.blob(b"creates the tmp dir")
    tmp = world.tmp / "cas" / DOMAIN / "tmp"
    old, fresh = tmp / "old.tmp", tmp / "fresh.tmp"
    for path, age_h in ((old, 25), (fresh, 23)):
        path.write_bytes(b"partial upload")
        stamp = (world.clock.now() - timedelta(hours=age_h)).timestamp()
        os.utime(path, (stamp, stamp))
    dry = collect(world.store, world.cas, clock=world.clock, dry_run=True)
    assert dry.tmp_removed == 1
    assert old.exists()
    real = collect(world.store, world.cas, clock=world.clock)
    assert real.tmp_removed == 1
    assert not old.exists()
    assert fresh.exists()


class _Hooked(InMemoryMetadataStore):
    """Runs `before[method]` once, just before that GC write (a build acting mid-sweep)."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(clock=clock)
        self.before: dict[str, Callable[[], object]] = {}

    def _hook(self, name: str) -> None:
        hook = self.before.pop(name, None)
        if hook is not None:
            hook()

    def gc_delete_cache_entries(
        self, domain: str, keys: Iterable[Digest], *, unused_before: datetime
    ) -> list[Digest]:
        self._hook("gc_delete_cache_entries")
        return super().gc_delete_cache_entries(domain, keys, unused_before=unused_before)

    def gc_claim(self, domain: str, digest: Digest, now: datetime) -> bool:
        self._hook("gc_claim")
        return super().gc_claim(domain, digest, now)


def hooked(world: World) -> _Hooked:
    store = _Hooked(world.clock)
    world.store = store
    return store


# R4/R7: a cache hit between the snapshot and the row deletion keeps the row and its objects.
def test_hit_during_row_deletion(world: World) -> None:
    store = hooked(world)
    tree = world.tree({"a.v": b"a", "b/c.v": b"c"})
    key = world.entry("k", {"model": tree})
    world.advance(days=31)
    store.before["gc_delete_cache_entries"] = lambda: store.cache_get(DOMAIN, key)
    report = collect(store, world.cas, clock=world.clock)
    assert report.cache_rows_deleted == 0
    assert tombstoned(world) == set()
    assert key in cache_keys(world)


# R4/R7: a tree touched between the snapshot and its claim stays, with everything below it.
def test_touch_during_byte_deletion(world: World) -> None:
    store = hooked(world)
    tree = world.tree({"a.v": b"a", "b/c.v": b"c"})
    world.advance(days=31)
    collect(store, world.cas, clock=world.clock)
    assert tree in tombstoned(world)
    world.advance(hours=25)
    store.before["gc_claim"] = lambda: store.touch(DOMAIN, [tree])
    report = collect(store, world.cas, clock=world.clock)
    assert report.deleted == 0
    world.cas.materialize(tree, "tree", world.tmp / "out")  # complete


# R2/R7 (I15): a lease or a pin taken after the snapshot, before the claim, keeps the object.
@pytest.mark.parametrize("root", ["lease", "pin"])
def test_root_taken_during_byte_deletion(world: World, root: str) -> None:
    store = hooked(world)
    out = world.blob(b"an old output")
    done = world.build()
    world.record(done, "gen", {"out": out})
    store.finish_build(done, "passed")
    world.advance(days=31)
    collect(store, world.cas, clock=world.clock)
    assert out in tombstoned(world)
    world.advance(hours=25)
    running = world.build()
    if root == "lease":
        store.before["gc_claim"] = lambda: store.acquire_lease(DOMAIN, running, [out], 900)
    else:
        store.before["gc_claim"] = lambda: store.pin_build(done, by="alice")
    report = collect(store, world.cas, clock=world.clock)
    assert report.deleted == 0
    assert out in present(world.cas)


# R5/R7: a quota row hit after the snapshot stays.
def test_hit_during_quota_row_deletion(world: World) -> None:
    store = hooked(world)
    store.set_policy(DOMAIN, DomainPolicy(quota_bytes=10, high_water=0.5, low_water=0.1))
    out = world.blob(b"x" * 40)
    key = world.entry("k", {"out": out})
    world.advance(days=2)
    store.before["gc_delete_cache_entries"] = lambda: store.cache_get(DOMAIN, key)
    report = collect(store, world.cas, clock=world.clock)
    assert report.quota is not None
    assert report.quota.evicted == [str(out)]  # planned, but the hit kept the row...
    assert key in cache_keys(world)
    assert out not in tombstoned(world)  # ...and so its output
