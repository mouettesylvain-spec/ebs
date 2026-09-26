from __future__ import annotations

import pytest

from ebs.core.clock import FakeClock
from tests.helpers.wait import WaitTimeout, wait_until


def test_returns_as_soon_as_predicate_holds(fake_clock: FakeClock) -> None:
    calls: list[float] = []

    def ready() -> bool:
        calls.append(fake_clock.monotonic())
        return len(calls) == 3

    wait_until(ready, timeout=10, interval=0.5, clock=fake_clock)
    assert calls == [0.0, 0.5, 1.0]


def test_times_out_with_description(fake_clock: FakeClock) -> None:
    with pytest.raises(WaitTimeout, match="job finished"):
        wait_until(lambda: False, timeout=2, interval=0.5, clock=fake_clock, what="job finished")
    assert fake_clock.monotonic() >= 2.0


def test_checks_once_more_at_deadline(fake_clock: FakeClock) -> None:
    def ready() -> bool:
        return fake_clock.monotonic() >= 2.0

    wait_until(ready, timeout=2, interval=5, clock=fake_clock)
