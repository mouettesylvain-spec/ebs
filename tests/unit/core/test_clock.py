from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ebs.core.clock import Clock, FakeClock, SystemClock

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# R8
def test_fake_clock_starts_at_given_time() -> None:
    clock = FakeClock(start=START, monotonic_start=100.0)
    assert clock.now() == START
    assert clock.monotonic() == 100.0


# R8
def test_advance_moves_wall_and_monotonic_time_together() -> None:
    clock = FakeClock(start=START)
    before = clock.monotonic()
    clock.advance(90.5)
    assert clock.now() == START + timedelta(seconds=90.5)
    assert clock.monotonic() == before + 90.5


# R8
def test_sleep_advances_instead_of_waiting() -> None:
    clock = FakeClock(start=START)
    clock.sleep(3600)  # would take an hour with a real clock
    assert clock.now() == START + timedelta(hours=1)
    assert clock.monotonic() == 3600.0


# R8
def test_now_is_timezone_aware_utc() -> None:
    assert FakeClock().now().tzinfo is UTC
    assert SystemClock().now().tzinfo is UTC


# R8
def test_naive_start_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FakeClock(start=datetime(2026, 1, 1))


# R8
@pytest.mark.parametrize("seconds", [-1.0, -0.001])
def test_negative_durations_are_rejected(seconds: float) -> None:
    clock = FakeClock(start=START)
    with pytest.raises(ValueError, match="negative"):
        clock.advance(seconds)
    with pytest.raises(ValueError, match="negative"):
        clock.sleep(seconds)
    assert clock.now() == START


# R8
def test_sleep_zero_is_a_no_op() -> None:
    clock = FakeClock(start=START)
    clock.sleep(0)
    assert clock.now() == START


def test_fake_clock_fixture_is_a_fake_clock(fake_clock: FakeClock) -> None:
    fake_clock.sleep(5)
    assert fake_clock.monotonic() == 5.0


def test_both_clocks_satisfy_protocol() -> None:
    clocks: list[Clock] = [FakeClock(), SystemClock()]
    for clock in clocks:
        assert isinstance(clock, Clock)


def test_system_clock_monotonic_does_not_go_backwards() -> None:
    clock = SystemClock()
    first = clock.monotonic()
    clock.sleep(0)
    assert clock.monotonic() >= first


# R8
def test_non_utc_start_is_normalised_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    clock = FakeClock(start=datetime(2026, 1, 1, 14, 0, tzinfo=plus_two))
    assert clock.now().utcoffset() == timedelta(0)
    assert clock.now() == START + timedelta(0)


def test_system_clock_uses_monotonic_time_and_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: 42.0)
    monkeypatch.setattr(time, "time", lambda: 1.0)
    monkeypatch.setattr(time, "sleep", slept.append)
    clock = SystemClock()
    assert clock.monotonic() == 42.0
    clock.sleep(0.25)
    assert slept == [0.25]
