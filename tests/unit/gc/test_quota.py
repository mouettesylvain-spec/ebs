"""Quota pressure (P1-09 R5): LRU eviction of unrooted objects from high to low water."""

from __future__ import annotations

from ebs.core.digest import Digest
from ebs.gc.run import collect
from ebs.meta.api import LEASE_TTL_S, DomainPolicy
from tests.helpers.gc import DOMAIN, World, present


def fill(world: World, n: int = 10) -> list[Digest]:
    """`n` blobs of 100 bytes, one per day, the last one just now: oldest first."""
    blobs = []
    for i in range(n):
        world.advance(days=1)
        blobs.append(world.blob(f"{i:03d}".encode() * 33 + b"!"))
    return blobs


# R5
def test_lru_until_low_water(world: World) -> None:
    world.store.set_policy(DOMAIN, DomainPolicy(quota_bytes=1000, high_water=0.8, low_water=0.5))
    blobs = fill(world)
    assert sum(size for _, size in world.cas.iter_digests()) == 1000
    entry = world.entry("uses-2", {"out": blobs[2]})
    world.advance(hours=2)  # the row was last used before the touch interval: deletable
    lease_holder = world.build()
    world.store.acquire_lease(DOMAIN, lease_holder, [blobs[0]], ttl_s=LEASE_TTL_S)  # rooted

    report = collect(world.store, world.cas, clock=world.clock)
    assert report.quota is not None
    assert report.quota.usage_bytes == 1000
    assert report.quota.high_water_bytes == 800
    assert report.quota.low_water_bytes == 500
    # Least recently used first, skipping the leased one, until usage <= low water.
    assert report.quota.evicted == [str(d) for d in blobs[1:6]]
    assert report.quota.evicted_bytes == 500
    assert entry not in {e.key for e in world.store.gc_cache_entries(DOMAIN)}  # rows first
    assert present(world.cas) == set(blobs)  # bytes after the grace period (I15)

    world.advance(hours=25)
    world.store.acquire_lease(DOMAIN, lease_holder, [blobs[0]], ttl_s=LEASE_TTL_S)
    again = collect(world.store, world.cas, clock=world.clock)
    assert present(world.cas) == {blobs[0], *blobs[6:]}
    assert again.deleted_bytes == 500
    assert again.quota is not None
    assert again.quota.evicted == []


# R5: rooted objects and objects used within the grace period are never evicted, even if the
# low water mark cannot be reached; pending deletions count as already freed.
def test_quota_never_evicts_rooted_or_recent(world: World) -> None:
    world.store.set_policy(DOMAIN, DomainPolicy(quota_bytes=1000, high_water=0.5, low_water=0.1))
    blobs = fill(world)
    pinned = world.build(plan=blobs[1])
    world.store.finish_build(pinned, "passed")
    world.store.pin_build(pinned, by="alice")
    world.store.touch(DOMAIN, [blobs[3]])  # used just now
    report = collect(world.store, world.cas, clock=world.clock)
    assert report.quota is not None
    evicted = set(report.quota.evicted)
    assert str(blobs[1]) not in evicted
    assert str(blobs[3]) not in evicted
    assert str(blobs[9]) not in evicted  # stored within the grace period
    assert evicted == {str(d) for i, d in enumerate(blobs) if i not in (1, 3, 9)}
    # The next run counts them as pending and evicts nothing more.
    again = collect(world.store, world.cas, clock=world.clock)
    assert again.quota is not None
    assert again.quota.evicted == []


# R5: without a quota there is no eviction and no quota section in the report.
def test_no_quota(world: World) -> None:
    fill(world, 3)
    report = collect(world.store, world.cas, clock=world.clock)
    assert report.quota is None
    assert report.tombstoned == 0


# R5: a tree is evicted together with what only it holds; a blob another tree holds stays.
def test_tree_evicted_with_its_subtree(world: World) -> None:
    world.store.set_policy(DOMAIN, DomainPolicy(quota_bytes=100, high_water=0.5, low_water=0.4))
    old = world.tree({"own.v": b"o" * 40, "shared.v": b"s" * 40}, register="all")
    world.advance(days=2)
    recent = world.tree({"shared.v": b"s" * 40, "new.v": b"n" * 10})
    report = collect(world.store, world.cas, clock=world.clock)
    assert report.quota is not None
    evicted = set(report.quota.evicted)
    entries = {e.name: e.digest for e in world.cas.get_tree(old).entries}
    assert {str(old), str(entries["own.v"])} <= evicted
    assert str(entries["shared.v"]) not in evicted  # `recent`, used just now, holds it
    assert str(recent) not in evicted
