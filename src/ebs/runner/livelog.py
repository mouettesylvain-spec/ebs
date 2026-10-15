"""The live log: the tool's output appended to `/debug/…/live.log` while it runs (decision D2).

`ebs logs -f` tails this file from a login node, so it is on NFS. Writes are batched: buffered
output goes out at most every `FLUSH_INTERVAL_S` seconds (or sooner when the buffer fills), so
a chatty tool costs a few NFS writes, not one per line. The file is bounded by `max_bytes`;
past it a note says so (the whole tail stays in `tool.log`). The runner removes the live log
after a passed run unless debug files are kept on success (P1-04 R3).

A live log is a convenience: a write error stops it with a warning and never fails the action.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Final

from ebs.core.clock import Clock
from ebs.core.log import get_logger
from ebs.runner.collect import open_debug_file

__all__ = ["FLUSH_INTERVAL_S", "LIVE_LOG_MAX", "LiveLog"]

log = get_logger(__name__)

FLUSH_INTERVAL_S: Final = 5.0
LIVE_LOG_MAX: Final = 256 << 20  # bytes
_MAX_BUFFER: Final = 1 << 20  # flush early beyond this, so memory stays bounded
_STOPPED_NOTE: Final = (
    "\n[ebs-runner: live log stopped after {limit} bytes; the end of the log is in tool.log]\n"
)


class LiveLog:
    """Append-only, periodically flushed copy of the tool's output. Thread-safe."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Clock,
        interval_s: float = FLUSH_INTERVAL_S,
        max_bytes: int = LIVE_LOG_MAX,
    ) -> None:
        self.path = path
        self._clock = clock
        self._interval = interval_s
        self._max = max_bytes
        self._lock = threading.Lock()
        self._buffer = bytearray()
        self._written = 0
        self._full = False  # max_bytes reached: further output is dropped
        self._fd: int | None = open_debug_file(
            path
        )  # exists at once, for `-f`; a retry restarts it
        self._last_flush = clock.monotonic()

    def write(self, data: bytes) -> None:
        """Buffer `data`; flush if a flush is due."""
        with self._lock:
            if self._fd is None or self._full:
                return
            room = self._max - self._written - len(self._buffer)
            if len(data) >= room:
                self._buffer += data[: max(0, room)]
                self._buffer += _STOPPED_NOTE.format(limit=self._max).encode()
                self._full = True
                self._flush()
                return
            self._buffer += data
            if len(self._buffer) >= _MAX_BUFFER:
                self._flush()
            else:
                self._flush_if_due()

    def tick(self) -> None:
        """Flush if `interval_s` passed since the last flush (called from the run loop)."""
        with self._lock:
            self._flush_if_due()

    def close(self) -> None:
        """Flush what is left and close the file."""
        with self._lock:
            self._flush()
            if self._fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._fd)
                self._fd = None

    def size(self) -> int:
        """Bytes in the file (0 once removed)."""
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def remove(self) -> None:
        """Close and delete the file (after a passed run)."""
        self.close()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()

    def _flush_if_due(self) -> None:
        if self._clock.monotonic() - self._last_flush >= self._interval:
            self._flush()

    def _flush(self) -> None:
        self._last_flush = self._clock.monotonic()
        if self._fd is None or not self._buffer:
            return
        data = bytes(self._buffer)
        self._buffer.clear()
        try:
            view = memoryview(data)
            while view:  # a write may be partial
                view = view[os.write(self._fd, view) :]
            self._written += len(data)
        except OSError as exc:
            log.warning(
                "live log write failed; live log stopped", path=str(self.path), error=str(exc)
            )
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = None
