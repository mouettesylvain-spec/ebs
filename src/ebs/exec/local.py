"""Local executor: `ebs-runner` subprocesses on this machine with bounded parallelism (P0-14).

Used for phase 0 builds, tests and `ebs reproduce`. Scheduling happens inside `submit` and `poll`
(no threads): finished runners are reaped, then queued actions start in FIFO order while their
`resources.cpus` fit in the free slots. A head action that does not fit blocks the ones behind
it, so a wide action is never starved by a stream of narrow ones. An action asking for more cpus
than `max_parallel` is capped to `max_parallel` (it runs alone) with a warning.

Memory limits are advisory here: `resources.mem` is logged with `enforced=False`, never applied
(no cgroups, no rlimits). Only the SLURM executor enforces memory.

Each runner starts in its own session with stdin closed; its stdout and stderr go to files under
`<log_dir>/local-*/` that are kept for debugging (`log_paths`). Without a build, the runner prints
its ResultManifest on stdout, so that file holds the result. The runner's stderr carries the
infra-failure line parsed for exit 75 (interfaces.md § 9).

`close` SIGKILLs a runner that ignores SIGTERM past the grace period. That kills the runner's
session only: the tool runs in a session of its own (P0-13), so a tool still running at that
moment is orphaned. The runner kills the tool's group as soon as SIGTERM arrives, so this needs
a runner stuck elsewhere.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import os
import signal
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from ebs.core.errors import ExecutorError
from ebs.core.log import get_logger
from ebs.exec.api import (
    TERMINAL_JOB_STATES,
    JobHandle,
    JobStatus,
    SubmitBatch,
    status_for_exit,
)
from ebs.plan.types import ActionSpec
from ebs.runner.errors import EXIT_INFRA, parse_infra_reason

__all__ = ["DEFAULT_CLOSE_GRACE_S", "LocalExecutor"]

log = get_logger(__name__)

DEFAULT_CLOSE_GRACE_S = 30.0
_STDERR_TAIL = 64 * 1024  # the infra line is among the runner's last words
_PENDING = JobStatus(state="pending", pending_reason="resources")
_RUNNING = JobStatus(state="running")
_CANCELLED = JobStatus(state="cancelled")


@dataclass(eq=False)
class _Job:
    handle: JobHandle
    argv: list[str]
    slots: int
    stdout: Path
    stderr: Path
    status: JobStatus = _PENDING
    proc: subprocess.Popen[bytes] | None = field(default=None, repr=False)


class LocalExecutor:
    """Runs each action as `<runner_argv> --plan … --action … --domain … [--build …]`."""

    name = "local"

    def __init__(
        self,
        *,
        log_dir: Path,
        runner_argv: Sequence[str] = ("ebs-runner",),
        max_parallel: int | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        if not runner_argv:
            raise ExecutorError("runner_argv is empty: give the ebs-runner command to start")
        slots = max_parallel if max_parallel is not None else (os.cpu_count() or 1)
        if slots < 1:
            raise ExecutorError(f"max_parallel must be at least 1, got {slots}")
        self.max_parallel = slots
        self._runner_argv = list(runner_argv)
        self._env = dict(os.environ if env is None else env)
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_dir = Path(tempfile.mkdtemp(prefix="local-", dir=log_dir))
        self._ids = itertools.count(1)
        self._jobs: dict[JobHandle, _Job] = {}
        self._queue: deque[_Job] = deque()
        self._running: dict[JobHandle, _Job] = {}
        self._closed = False

    # --- Executor --------------------------------------------------------------------------------

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        if self._closed:
            raise ExecutorError("the local executor is closed; create a new one to submit")
        jobs = [self._job(batch, action) for action in batch.actions]
        for job in jobs:
            self._jobs[job.handle] = job
            self._queue.append(job)
        self._schedule()
        return [job.handle for job in jobs]

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        jobs = [self._lookup(h) for h in handles]
        self._reap()
        self._schedule()
        return {job.handle: job.status for job in jobs}

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        """SIGTERM running runners, drop queued ones; runners that already exited keep their state.

        A runner may have posted its result and still be running when cancelled; the driver
        must tolerate a result for a job reported `cancelled`.
        """
        jobs = [self._lookup(h) for h in handles]
        self._reap()  # a runner that exited before this call is done (or failed), not cancelled
        for job in jobs:
            if job.status.state in TERMINAL_JOB_STATES:
                continue
            job.status = _CANCELLED
            if job.proc is None:
                self._queue.remove(job)
            else:  # the runner kills the tool's group and removes its scratch (P0-13)
                with contextlib.suppress(ProcessLookupError):
                    job.proc.send_signal(signal.SIGTERM)
                log.info("runner cancelled", action_id=job.handle.action_id, pid=job.proc.pid)
        self._reap()
        self._schedule()

    # --- lifecycle -------------------------------------------------------------------------------

    def close(self, grace_s: float = DEFAULT_CLOSE_GRACE_S) -> None:
        """Cancel all unfinished work and wait for the runners; SIGKILL those alive after `grace_s`.

        The grace period covers the whole close, not each runner.
        """
        self._closed = True
        self.cancel([h for h, j in self._jobs.items() if j.status.state not in TERMINAL_JOB_STATES])
        deadline = time.monotonic() + grace_s
        for job in list(self._running.values()):
            assert job.proc is not None
            try:
                job.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning(
                    "runner ignored SIGTERM; killing it",
                    action_id=job.handle.action_id,
                    grace_s=grace_s,
                )
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(job.proc.pid, signal.SIGKILL)
                job.proc.wait()
        self._reap()

    def __enter__(self) -> LocalExecutor:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def log_paths(self, handle: JobHandle) -> tuple[Path, Path]:
        """The (stdout, stderr) files of the runner behind `handle`."""
        job = self._lookup(handle)
        return job.stdout, job.stderr

    # --- internals -------------------------------------------------------------------------------

    def _lookup(self, handle: JobHandle) -> _Job:
        job = self._jobs.get(handle)
        if job is None:
            raise ExecutorError(
                f"unknown job handle {handle}: it was not submitted to this local executor"
            )
        return job

    def _job(self, batch: SubmitBatch, action: ActionSpec) -> _Job:
        job_id = str(next(self._ids))
        handle = JobHandle(executor=self.name, job_id=job_id, action_id=action.action_id)
        argv = [
            *self._runner_argv,
            "--plan", str(batch.plan),
            "--action", action.action_id,
            "--domain", batch.domain,
        ]  # fmt: skip
        if batch.build is not None:
            argv += ["--build", str(batch.build)]
        stem = f"{job_id}-{hashlib.sha256(action.action_id.encode()).hexdigest()[:16]}"
        return _Job(
            handle=handle,
            argv=argv,
            slots=self._slots(action),
            stdout=self._log_dir / f"{stem}.out",
            stderr=self._log_dir / f"{stem}.err",
        )

    def _slots(self, action: ActionSpec) -> int:
        cpus = action.resources.cpus
        mem = action.resources.mem
        if mem is not None:
            log.info(
                "memory request is advisory on the local executor",
                action_id=action.action_id,
                mem=mem,
                enforced=False,
            )
        if cpus is None:
            return 1
        if not isinstance(cpus, int) or cpus < 1:
            raise ExecutorError(
                f"action {action.action_id}: resources.cpus is {cpus!r}, not a positive "
                "integer; the planner must expand it before submission"
            )
        if cpus > self.max_parallel:
            log.warning(
                "action asks for more cpus than the local executor has; it will run alone",
                action_id=action.action_id,
                cpus=cpus,
                max_parallel=self.max_parallel,
            )
            return self.max_parallel
        return cpus

    def _free_slots(self) -> int:
        return self.max_parallel - sum(j.slots for j in self._running.values())

    def _schedule(self) -> None:
        while self._queue and self._queue[0].slots <= self._free_slots():
            self._start(self._queue.popleft())

    def _start(self, job: _Job) -> None:
        try:
            with job.stdout.open("wb") as out, job.stderr.open("wb") as err:
                job.proc = subprocess.Popen(
                    job.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=self._env,
                    start_new_session=True,  # a Ctrl-C reaches the driver, which cancels
                    close_fds=True,
                )
        except OSError as exc:
            log.error("cannot start the runner", action_id=job.handle.action_id, error=str(exc))
            job.status = JobStatus(state="infra_failed", infra_reason="runner_crash")
            return
        job.status = _RUNNING
        self._running[job.handle] = job
        log.debug("runner started", action_id=job.handle.action_id, pid=job.proc.pid)

    def _reap(self) -> None:
        for job in list(self._running.values()):
            assert job.proc is not None
            code = job.proc.poll()
            if code is None:
                continue
            del self._running[job.handle]
            if job.status.state == "cancelled":
                continue
            reason = parse_infra_reason(self._stderr_tail(job)) if code == EXIT_INFRA else None
            job.status = status_for_exit(code, reason)
            log.debug("runner exited", action_id=job.handle.action_id, exit_code=code)

    @staticmethod
    def _stderr_tail(job: _Job) -> str:
        try:
            with job.stderr.open("rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - _STDERR_TAIL))
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""
