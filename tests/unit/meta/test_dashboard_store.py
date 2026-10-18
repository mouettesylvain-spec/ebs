"""Dashboard reads of the in-memory store (P2-06); the same contract runs on PostgreSQL."""

from __future__ import annotations

from datetime import timedelta

from ebs.core.clock import FakeClock
from ebs.meta._common import aggregate_stats
from ebs.meta.api import BuildId, Event, PendingWait
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.dashboard import (
    check_domain_stats,
    check_list_builds,
    check_result_summaries,
)


# R1: builds of a domain, newest first, filtered by user, status, project and date.
def test_list_builds() -> None:
    clock = FakeClock()
    check_list_builds(InMemoryMetadataStore(clock=clock), clock)


# R4: cache hits and the CPU time they saved per step; pending waits per reason and feature.
def test_domain_stats() -> None:
    clock = FakeClock()
    check_domain_stats(InMemoryMetadataStore(clock=clock), clock)


# R2: the results of a build's actions, for the regression grid.
def test_result_summaries() -> None:
    check_result_summaries(InMemoryMetadataStore())


T0 = FakeClock().now()


def _ev(build: int, action: str, type_: str, at: int, **data: object) -> Event:
    return Event(ts=T0 + timedelta(seconds=at), build=BuildId(build), type=type_,  # type: ignore[arg-type]
                 action_id=action, data=data)  # fmt: skip


def _waits(events: list[Event], *, until_s: int = 3600, now_s: int = 3600) -> list[PendingWait]:
    return aggregate_stats(
        "dash", since=T0, until=T0 + timedelta(seconds=until_s), now=T0 + timedelta(seconds=now_s),
        builds=1, actions=[], events=events,
    ).waits  # fmt: skip


# R4 (review): two builds running the same action id wait separately.
def test_stats_waits_per_build() -> None:
    waits = _waits([
        _ev(1, "sim[1]", "pending", 0, reason="resources"),
        _ev(1, "sim[1]", "running", 100),
        _ev(2, "sim[1]", "pending", 10, reason="resources"),
        _ev(2, "sim[1]", "running", 20),
    ])  # fmt: skip
    assert waits == [PendingWait(reason="resources", feature=None, waits=2, wait_s=110)]


# R4 (review): events may come in any order (the PostgreSQL query does not sort them).
def test_stats_events_out_of_order() -> None:
    waits = _waits([
        _ev(1, "a", "running", 60),
        _ev(1, "a", "pending", 0, reason="licenses", licenses=["f", "f"]),  # one feature, once
        _ev(1, "a", "pending", 30, reason="licenses", licenses=["f"]),
    ])  # fmt: skip
    assert waits == [PendingWait(reason="licenses", feature="f", waits=1, wait_s=60)]


# R4 (review): a closed wait is cut at `until` too; a wait starting after it counts 0 s.
def test_stats_waits_clamped_to_window() -> None:
    events = [
        _ev(1, "a", "pending", 0, reason="resources"),
        _ev(1, "a", "running", 60),
        _ev(1, "b", "pending", 200, reason="other"),
    ]
    assert _waits(events, until_s=40) == [
        PendingWait(reason="other", feature=None, waits=1, wait_s=0),
        PendingWait(reason="resources", feature=None, waits=1, wait_s=40),
    ]
