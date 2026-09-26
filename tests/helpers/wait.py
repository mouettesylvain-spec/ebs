"""Poll-with-timeout helper; the only sanctioned way for tests to wait for something."""

from __future__ import annotations

from collections.abc import Callable

from ebs.core.clock import Clock, SystemClock


class WaitTimeout(AssertionError):
    """The awaited condition did not become true in time."""


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 10.0,
    interval: float = 0.05,
    clock: Clock | None = None,
    what: str = "condition",
) -> None:
    """Call `predicate` every `interval` seconds until it is true; fail after `timeout` seconds."""
    clock = clock or SystemClock()
    deadline = clock.monotonic() + timeout
    while True:
        if predicate():
            return
        remaining = deadline - clock.monotonic()
        if remaining <= 0:
            raise WaitTimeout(f"timed out after {timeout} s waiting for {what}")
        clock.sleep(min(interval, remaining))
