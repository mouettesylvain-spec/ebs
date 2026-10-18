"""One GC run per domain (P1-09 follow-up): `collect` holds the domain's GC lock."""

from __future__ import annotations

from datetime import timedelta

import pytest

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
