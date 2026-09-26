"""Injectable clocks. Code takes a `Clock` instead of calling `time`/`datetime` directly."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current wall-clock time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds on a monotonic clock, for measuring durations and timeouts."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait for `seconds`."""
        ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


_DEFAULT_START = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    """Deterministic clock for tests: time moves only through `advance()` or `sleep()`."""

    def __init__(self, start: datetime = _DEFAULT_START, monotonic_start: float = 0.0) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware (use tzinfo=UTC)")
        self._now = start.astimezone(UTC)
        self._monotonic = monotonic_start

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError(f"cannot advance a clock by a negative duration ({seconds} s)")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds

    def sleep(self, seconds: float) -> None:
        """Advance by `seconds` instead of waiting."""
        self.advance(seconds)
