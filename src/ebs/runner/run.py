"""Run the tool: own process group, wall-clock timeout, capped combined log, rusage.

The tool starts in a new session (so it is the leader of its own process group) with stdin
closed and stdout+stderr on one pipe. A reader thread copies the pipe to `logs/tool.log`,
keeping at most `max_log` bytes: the *tail*, because the end of a log explains a failure. On
timeout the whole group gets SIGTERM, then SIGKILL after `grace_s`. Whatever way the run ends
(normal exit, timeout, or an exception such as `Interrupted` from a signal handler), no process
of the group is left behind.
"""

from __future__ import annotations

import contextlib
import os
import resource
import signal
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from ebs.core.log import get_logger

__all__ = ["DROPPED_MARKER", "ToolRun", "kill_group", "run_tool"]

log = get_logger(__name__)

DROPPED_MARKER: Final = "[ebs-runner: {dropped} bytes of log dropped, only the tail is kept]\n"
_CHUNK: Final = 1 << 16
_POLL_MIN_S: Final = 0.01
_POLL_MAX_S: Final = 0.25
_READER_JOIN_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class ToolRun:
    exit_code: int  # -N when killed by signal N (subprocess convention)
    timed_out: bool
    wall_s: float
    cpu_s: float  # user + system, including waited-for descendants
    max_rss_kb: int  # ru_maxrss (KiB on Linux)
    log_dropped: int  # bytes cut from the head of the log by the cap
    exec_error: str | None  # the program could not be started (nothing ran)
    log_error: str | None = None  # the log could not be written (e.g. scratch disk full)


def run_tool(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout_s: float | None,
    max_log: int,
    grace_s: float,
) -> ToolRun:
    log_file = _CappedLog(log_path, max_log)
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        message = f"ebs-runner: cannot execute {argv[0]!r}: {exc}\n"
        log_file.write(message.encode())
        log_file.close()
        return ToolRun(127, False, time.monotonic() - start, 0.0, 0, 0, message.strip())

    assert proc.stdout is not None
    reader = threading.Thread(target=_copy, args=(proc.stdout, log_file), daemon=True)
    reader.start()
    timed_out = False
    status: int | None = None
    usage: resource.struct_rusage | None = None
    try:
        status, usage, timed_out = _wait(proc.pid, start, timeout_s, grace_s)
    finally:
        if status is None:  # an exception (e.g. Interrupted) while the tool runs
            kill_group(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.wait4(proc.pid, 0)
        else:
            # The leader exited; stragglers of its group would hold the scratch dir and the log.
            kill_group(proc.pid, signal.SIGKILL)
        proc.returncode = -signal.SIGKILL if status is None else os.waitstatus_to_exitcode(status)
        reader.join(_READER_JOIN_S)
        proc.stdout.close()
        try:
            log_file.close()
        except OSError as exc:
            log_file.error = log_file.error or f"cannot write the tool log {log_path}: {exc}"
    wall = time.monotonic() - start
    assert usage is not None
    assert status is not None
    return ToolRun(
        exit_code=os.waitstatus_to_exitcode(status),
        timed_out=timed_out,
        wall_s=wall,
        cpu_s=usage.ru_utime + usage.ru_stime,
        max_rss_kb=usage.ru_maxrss,
        log_dropped=log_file.dropped,
        exec_error=None,
        log_error=log_file.error,
    )


def _wait(
    pid: int, start: float, timeout_s: float | None, grace_s: float
) -> tuple[int, resource.struct_rusage, bool]:
    """Reap `pid`, enforcing the timeout; returns (wait status, rusage, timed out)."""
    deadline = None if timeout_s is None else start + timeout_s
    kill_at: float | None = None
    timed_out = False
    interval = _POLL_MIN_S
    while True:
        done, status, usage = os.wait4(pid, os.WNOHANG)
        if done:
            return status, usage, timed_out
        now = time.monotonic()
        if deadline is not None and not timed_out and now >= deadline:
            timed_out = True
            log.info("tool timed out; terminating its process group", pid=pid, grace_s=grace_s)
            kill_group(pid, signal.SIGTERM)
            kill_at = now + grace_s
            interval = _POLL_MIN_S
        if kill_at is not None and now >= kill_at:
            kill_group(pid, signal.SIGKILL)
            kill_at = None
        time.sleep(interval)
        interval = min(interval * 2, _POLL_MAX_S)


def kill_group(pgid: int, sig: int) -> None:
    """Send `sig` to process group `pgid`; a group that is already gone is fine."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _copy(pipe: BinaryIO, out: _CappedLog) -> None:
    # Keep draining after a write error: a tool blocked on a full pipe would hang the run.
    while chunk := pipe.read1(_CHUNK):  # type: ignore[attr-defined]
        if out.error is not None:
            continue
        try:
            out.write(chunk)
        except OSError as exc:
            out.error = f"cannot write the tool log {out.path}: {exc}"


class _CappedLog:
    """Writes a log keeping only its last `cap` bytes, with bounded memory.

    Bytes go to `<log>.cur`; when it would exceed `cap` it becomes `<log>.prev` and a new
    `.cur` starts. On close the tail of `.prev` and all of `.cur` (at most `cap` bytes together)
    are joined into `<log>`, after a marker saying how much was dropped.
    """

    def __init__(self, path: Path, cap: int) -> None:
        if cap <= 0:
            raise ValueError(f"log cap must be positive, got {cap}")
        self.path, self.cap = path, cap
        self._cur_path = path.with_name(path.name + ".cur")
        self._prev_path = path.with_name(path.name + ".prev")
        self._cur = open(self._cur_path, "wb")  # noqa: SIM115 - closed in close()
        self._cur_size = 0
        self._prev_size = 0
        self.total = 0
        self.dropped = 0
        self.error: str | None = None

    def write(self, data: bytes) -> None:
        self.total += len(data)
        while data:
            room = self.cap - self._cur_size
            if room == 0:
                self._rotate()
                room = self.cap
            part, data = data[:room], data[room:]
            self._cur.write(part)
            self._cur_size += len(part)

    def _rotate(self) -> None:
        self._cur.close()
        os.replace(self._cur_path, self._prev_path)
        self._prev_size = self._cur_size
        self._cur = open(self._cur_path, "wb")  # noqa: SIM115
        self._cur_size = 0

    def close(self) -> None:
        if self._cur.closed:
            return
        self._cur.close()
        if self._prev_size == 0:
            os.replace(self._cur_path, self.path)
            return
        keep_prev = self.cap - self._cur_size
        self.dropped = self.total - self.cap
        with open(self.path, "wb") as out:
            out.write(DROPPED_MARKER.format(dropped=self.dropped).encode())
            with open(self._prev_path, "rb") as prev:
                prev.seek(self._prev_size - keep_prev)
                _copy_file(prev, out)
            with open(self._cur_path, "rb") as cur:
                _copy_file(cur, out)
        os.unlink(self._prev_path)
        os.unlink(self._cur_path)


def _copy_file(src: BinaryIO, dst: BinaryIO) -> None:
    while chunk := src.read(1 << 20):
        dst.write(chunk)
