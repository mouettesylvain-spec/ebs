"""One GC run per domain (P1-09 follow-up): `collect` holds the domain's GC lock."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from ebs.core.digest import Digest
from ebs.core.errors import GcBusyError, MetadataError
from ebs.gc.run import GC_LOCK_TTL_S, collect
from tests.helpers.gc import DOMAIN, World, present


def test_second_run_refused(world: World) -> None:
    now = world.clock.now()
    world.store.gc_lock(DOMAIN, "other-host:7:x", now=now, ttl_s=600)
    blob = world.blob(b"old")
    world.advance(days=40)  # the lock expired meanwhile: this run takes it
    collect(world.store, world.cas, clock=world.clock)  # tombstones the blob
    world.advance(days=10)  # past the grace period: the next run would delete it
    world.store.gc_lock(DOMAIN, "other-host:7:x", now=world.clock.now(), ttl_s=600)
    with pytest.raises(GcBusyError, match="other-host:7:x"):
        collect(world.store, world.cas, clock=world.clock)
    assert blob in present(world.cas)  # the refused run deleted nothing
    collect(world.store, world.cas, clock=world.clock, dry_run=True)  # a dry run takes no lock
    world.store.gc_unlock(DOMAIN, "other-host:7:x")
    collect(world.store, world.cas, clock=world.clock)
    assert blob not in present(world.cas)


def test_lock_released_after_the_run(world: World) -> None:
    collect(world.store, world.cas, clock=world.clock)
    held = world.store.gc_lock(DOMAIN, "next:1:y", now=world.clock.now(), ttl_s=600)
    assert held.owner == "next:1:y"


def test_lock_held_during_the_run_and_released_on_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    def fail(*_: object, **__: object) -> None:
        held = world.store.gc_lock(DOMAIN, "probe:1:z", now=world.clock.now(), ttl_s=600)
        seen.append(held)
        raise MetadataError("database gone")

    monkeypatch.setattr(world.store, "gc_expire_leases", fail)
    with pytest.raises(MetadataError, match="database gone"):
        collect(world.store, world.cas, clock=world.clock)
    (held,) = seen
    assert held.owner != "probe:1:z"  # the run held it
    assert held.expires_at == world.clock.now() + timedelta(seconds=GC_LOCK_TTL_S)
    assert world.store.gc_lock(DOMAIN, "probe:1:z", now=world.clock.now(), ttl_s=600).owner == (
        "probe:1:z"
    )


def _due_blobs(world: World, n: int) -> list[Digest]:
    """`n` blobs whose tombstones are due: the next run deletes them."""
    blobs = [world.blob(f"old {i}".encode()) for i in range(n)]
    world.advance(days=40)
    collect(world.store, world.cas, clock=world.clock)  # tombstones them
    world.advance(days=10)  # past the grace period
    return blobs


# Review: the lock is renewed after the snapshot; one taken over meanwhile stops the run before
# it changes anything.
def test_lock_lost_during_the_snapshot(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    blobs = _due_blobs(world, 2)
    real = world.store.gc_policy

    def steal(domain: str) -> Any:
        world.advance(hours=7)  # this run's lock expired...
        world.store.gc_lock(DOMAIN, "thief:1:t", now=world.clock.now(), ttl_s=600)  # ...and went
        return real(domain)

    monkeypatch.setattr(world.store, "gc_policy", steal)
    with pytest.raises(GcBusyError, match="thief:1:t"):
        collect(world.store, world.cas, clock=world.clock)
    assert set(blobs) <= present(world.cas)


# Review: a long sweep renews its lock while it deletes, so a second run cannot take it over.
def test_lock_renewed_while_deleting(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    blobs = _due_blobs(world, 4)
    real, probes = world.store.gc_claim, []

    def slow_claim(domain: str, digest: Digest, now: datetime) -> bool:
        world.advance(hours=GC_LOCK_TTL_S / 3600 / 2)  # half the lock's TTL per object
        probes.append(world.store.gc_lock(DOMAIN, "next:2:n", now=world.clock.now(), ttl_s=600))
        return real(domain, digest, now)

    monkeypatch.setattr(world.store, "gc_claim", slow_claim)
    collect(world.store, world.cas, clock=world.clock)
    assert len(probes) == 4
    assert all(p.owner != "next:2:n" for p in probes)  # never free while the run went on
    assert not set(blobs) & present(world.cas)


# Review: a run whose lock was taken over mid-sweep stops there.
def test_lock_lost_while_deleting(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _due_blobs(world, 4)
    real, claims = world.store.gc_claim, []

    def claim(domain: str, digest: Digest, now: datetime) -> bool:
        claims.append(digest)
        if len(claims) == 2:
            world.advance(hours=7)
            world.store.gc_lock(DOMAIN, "thief:1:t", now=world.clock.now(), ttl_s=6 * 3600)
        return real(domain, digest, now)

    monkeypatch.setattr(world.store, "gc_claim", claim)
    with pytest.raises(GcBusyError, match="thief:1:t"):
        collect(world.store, world.cas, clock=world.clock)
    assert len(claims) == 2  # the third object was left to the run holding the lock
