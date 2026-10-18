"""EventReader contract (P2-05 R2, R6): `memory` as a unit test, `pg` as an integration test."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError
from ebs.meta.api import BuildId, Event, EventReader, StoredEvent
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.pg import PgMetadataStore
from ebs.release.releases import ReleaseBackend
from tests.helpers.pg import url_string
from tests.helpers.release import finish


class Store(ReleaseBackend, EventReader, Protocol):
    pass


@dataclass
class S:
    store: Store
    clock: FakeClock


@pytest.fixture(params=["memory", pytest.param("pg", marks=pytest.mark.integration)])
def s(request: pytest.FixtureRequest) -> Iterator[S]:
    clock = FakeClock()
    if request.param == "memory":
        yield S(InMemoryMetadataStore(clock=clock), clock)
        return
    url: URL = request.getfixturevalue("pg_url")
    pg = PgMetadataStore.from_config({"url": url_string(url)}, clock=clock)
    try:
        yield S(pg, clock)
    finally:
        pg.close()


def _emit(s: S, build: BuildId, type_: str = "running", ts: datetime | None = None) -> None:
    s.store.emit(
        Event(ts=ts or s.clock.now(), build=build, type=type_, action_id="a", data={"n": 1})  # type: ignore[arg-type]
    )


def _types(events: list[StoredEvent]) -> list[str]:
    return [e.event.type for e in events]


# R2: the stream's source: a build's or a domain's events after an id, oldest first.
def test_events_after_by_build_and_domain(s: S) -> None:
    one = finish(s.store, {}, status=None)
    two = finish(s.store, {}, status=None)
    other = finish(s.store, {}, status=None, domain="other")
    _emit(s, one, "build_started")
    _emit(s, two, "build_started")
    _emit(s, other, "build_started")
    _emit(s, one, "running")
    _emit(s, one, "build_finished")

    mine = s.store.events_after(0, build=one)
    assert _types(mine) == ["build_started", "running", "build_finished"]
    assert all(e.event.build == one for e in mine)
    ids = [e.id for e in mine]
    assert ids == sorted(ids)
    assert len(set(ids)) == 3
    assert mine[0].event.data == {"n": 1}

    # Resuming after an id returns only what follows it.
    assert _types(s.store.events_after(ids[0], build=one)) == ["running", "build_finished"]
    assert s.store.events_after(ids[-1], build=one) == []
    assert _types(s.store.events_after(0, build=one, limit=2)) == ["build_started", "running"]

    domain = s.store.events_after(0, domain="test")
    assert [e.event.build for e in domain] == [one, two, one, one]
    assert [e.event.build for e in s.store.events_after(0, domain="other")] == [other]
    assert s.store.events_after(0, domain="nowhere") == []


# R2
@pytest.mark.parametrize(
    "kwargs",
    [{}, {"build": BuildId(1), "domain": "test"}, {"build": BuildId(1), "limit": 0},
     {"build": BuildId(1), "limit": 1001}, {"domain": "Bad Domain"}],
)  # fmt: skip
def test_events_after_rejects_bad_queries(s: S, kwargs: dict[str, object]) -> None:
    with pytest.raises(MetadataError):
        s.store.events_after(0, **kwargs)  # type: ignore[arg-type]


def _advance_to(clock: FakeClock, when: datetime) -> None:
    clock.advance((when - clock.now()).total_seconds())


# R6: retention drops whole months (partitions) of when events were stored, never part of one.
# Far-future months: no other test stores events there (pg's session database is shared).
def test_prune_drops_whole_months_before_the_cutoff(s: S) -> None:
    build = finish(s.store, {}, status=None)
    for month in (7, 8, 9, 10):
        _advance_to(s.clock, datetime(2101, month, 15, tzinfo=UTC))
        _emit(s, build, "running", datetime(1999, 1, 1, tzinfo=UTC))  # ts is not the key

    def ours(months: list[str]) -> list[str]:
        return [m for m in months if m.startswith("2101-")]

    cutoff = datetime(2101, 9, 10, tzinfo=UTC)  # inside September: September is kept
    assert ours(s.store.prune_events(cutoff, dry_run=True)) == ["2101-07", "2101-08"]
    assert len(s.store.events_after(0, build=build)) == 4  # a dry run drops nothing
    assert ours(s.store.prune_events(cutoff)) == ["2101-07", "2101-08"]
    assert len(s.store.events_after(0, build=build)) == 2
    assert ours(s.store.prune_events(cutoff)) == []
    # A month that ends exactly at the cutoff is dropped.
    assert ours(s.store.prune_events(datetime(2101, 10, 1, tzinfo=UTC))) == ["2101-09"]
    assert len(s.store.events_after(0, build=build)) == 1
