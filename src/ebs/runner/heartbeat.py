"""Build-lease heartbeat while the tool runs (P1-04 R5).

The driver holds a GC lease on the build's artifacts (P1-09); a long action keeps it alive by
renewing it from the node. Until P1-09 provides the lease API the runner passes no `renew`
hook and the heartbeat does nothing. A failed renewal is logged and retried at the next beat:
it never stops the tool.
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
