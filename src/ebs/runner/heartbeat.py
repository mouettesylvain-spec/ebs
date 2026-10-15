"""Build-lease heartbeat while the tool runs (P1-04 R5).

Under `--build` the runner holds a GC lease (P1-09) on the inputs it staged and renews it from
the node through the `renew` hook (`Runner._lease_hook`). A failed renewal is logged and
retried at the next beat: it never stops the tool.
"""

from __future__ import annotations

from collections.abc import Callable

from ebs.core.clock import Clock
from ebs.core.errors import EbsError
from ebs.core.log import get_logger

__all__ = ["Heartbeat"]

log = get_logger(__name__)


class Heartbeat:
    """Calls `renew` on the first `tick()` and then once every `interval_s` seconds."""

    def __init__(self, clock: Clock, interval_s: float, renew: Callable[[], None] | None) -> None:
        if interval_s <= 0:
            raise ValueError(f"heartbeat interval must be positive, got {interval_s}")
        self._clock = clock
        self._interval = interval_s
        self._renew = renew
        self._next: float | None = None

    def tick(self) -> None:
        if self._renew is None:
            return
        now = self._clock.monotonic()
        if self._next is not None and now < self._next:
            return
        self._next = now + self._interval
        try:
            self._renew()
        except (EbsError, OSError) as exc:
            log.warning("lease renewal failed; retrying at the next heartbeat", error=str(exc))
