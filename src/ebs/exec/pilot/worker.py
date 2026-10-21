"""A pilot job (`ebs-pilot`): lease an action, run `ebs-runner` for it, report, repeat (P3-06 R1).

One pilot is one SLURM job (an array task) with a fixed allocation. It loops:

1. Lease the oldest queued item of its pool that fits what is left of its allocation
   (`Allocation.slot`: cpus, memory, licenses, node features, and walltime minus the runner's
   grace), with a visibility timeout of `lease_ttl_s`.
2. Run `ebs-runner --plan … --action … --domain … [--build …]` with its output in
   `<log dir>/<pilot>/<item>.out`, renewing the lease every third of its TTL. If a renewal says
   the lease is gone (the executor cancelled the action, or the lease expired because this pilot
   stalled), the runner is stopped: SIGTERM, then SIGKILL after `kill_grace_s` (it cleans its
   scratch on SIGTERM).
3. Report the runner's exit code, and its infra reason for exit 75 (the runner's last line).

It stops when it found no work for `idle_s` (it holds its licenses while idle, so keep that
short), when no action could fit the walltime it has left, or on SIGTERM (scancel or its time
limit): the running action gets the SIGTERM too, and its exit is reported before the pilot ends.

A pilot that dies without reporting leaves its lease to expire: the executor then reports that
action as an infrastructure failure (`node_fail`) and the driver retries it (R2).
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Final, Protocol
from uuid import UUID

from ebs.config import config_paths, load_config
from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import EbsError, MetadataError
from ebs.core.log import get_logger
from ebs.exec.pilot.queue import Allocation
from ebs.exec.slurm.jobscript import runner_arguments
from ebs.meta.api import InfraReason, PilotQueueStore, QueueLease
from ebs.meta.factory import open_metadata_store
from ebs.runner.errors import EXIT_INFRA, parse_infra_reason

__all__ = [
    "PilotConfig",
    "PilotWorker",
    "Process",
    "main",
    "parse_pilot_arguments",
    "pilot_arguments",
    "pilot_identity",
    "spawn_runner",
]

log = get_logger(__name__)

DEFAULT_KILL_GRACE_S: Final = 30.0
_LOG_TAIL: Final = 64 * 1024
EXIT_PILOT_USAGE: Final = 2
EXIT_PILOT_ERROR: Final = 1


class Process(Protocol):
    """The part of `subprocess.Popen` a pilot uses."""

    def poll(self) -> int | None: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


Spawn = Callable[[Sequence[str], Path, Mapping[str, str]], Process]


def spawn_runner(argv: Sequence[str], log_file: Path, env: Mapping[str, str]) -> Process:
    """Start the runner with stdout and stderr in `log_file` (argv list, no shell)."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("wb") as out:
        return subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=dict(env),
        )


@dataclass(frozen=True)
class PilotConfig:
    domain: str
    pool: UUID
    allocation: Allocation
    runner_argv: tuple[str, ...]
    log_dir: Path
    idle_s: float
    lease_ttl_s: int
    poll_s: float
    kill_grace_s: float = DEFAULT_KILL_GRACE_S


class PilotWorker:
    """The loop of one pilot. `clock.sleep` paces it (a FakeClock in unit tests)."""

    def __init__(
        self,
        store: PilotQueueStore,
        config: PilotConfig,
        *,
        pilot: str,
        node: str | None,
        clock: Clock | None = None,
        spawn: Spawn = spawn_runner,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._store = store
        self._cfg = config
        self._pilot = pilot
        self._node = node
        self._clock = clock or SystemClock()
        self._spawn = spawn
        self._env = dict(os.environ if env is None else env)
        self._started = self._clock.monotonic()
        self._stopping = False
        self._current: Process | None = None

    def stop(self) -> None:
        """Take no more work; the running action gets SIGTERM and is still reported."""
        self._stopping = True
        if self._current is not None:
            self._current.send_signal(signal.SIGTERM)

    def run(self) -> int:
        """Work until idle, out of walltime or stopped; the number of actions run."""
        cfg = self._cfg
        ran = 0
        idle_since = self._clock.monotonic()
        while not self._stopping:
            elapsed = self._clock.monotonic() - self._started
            slot = cfg.allocation.slot(self._pilot, node=self._node, elapsed_s=elapsed)
            if slot.time_left_s <= 0:
                log.info("pilot.walltime_used", pilot=self._pilot, ran=ran)
                break
            try:
                lease = self._store.queue_lease(cfg.domain, cfg.pool, slot, cfg.lease_ttl_s)
            except MetadataError as exc:  # counts as idle: never hold licenses for nothing
                log.warning("pilot.lease_failed", pilot=self._pilot, error=str(exc))
                lease = None
            if lease is None:
                if self._clock.monotonic() - idle_since >= cfg.idle_s:
                    log.info("pilot.idle", pilot=self._pilot, ran=ran)
                    break
                self._clock.sleep(cfg.poll_s)
                continue
            self._run(lease)
            ran += 1
            idle_since = self._clock.monotonic()
        return ran

    def _run(self, lease: QueueLease) -> None:
        cfg = self._cfg
        argv = runner_arguments(
            cfg.runner_argv,
            plan=lease.plan,
            domain=cfg.domain,
            build=lease.build,
            action=lease.action_id,
        )
        out = cfg.log_dir / self._pilot / f"{lease.item}.out"
        log.info("pilot.run", pilot=self._pilot, item=lease.item, action=lease.action_id)
        try:
            proc = self._spawn(argv, out, self._env)
        except OSError as exc:
            log.error("pilot.spawn_failed", pilot=self._pilot, argv=argv, error=str(exc))
            self._finish(lease, EXIT_INFRA, "runner_crash")
            self._stopping = True  # a pilot that cannot start runners must not drain the queue
            return
        self._current = proc
        if self._stopping:  # SIGTERM arrived while it started
            proc.send_signal(signal.SIGTERM)
        try:
            code = self._wait(proc, lease)
        finally:
            self._current = None
        if code is None:  # the lease is gone: nobody waits for this result
            return
        reason = parse_infra_reason(_tail(out)) if code == EXIT_INFRA else None
        self._finish(lease, code, reason)

    def _wait(self, proc: Process, lease: QueueLease) -> int | None:
        """The runner's exit code, or None if the lease was lost and the runner stopped."""
        cfg = self._cfg
        every = cfg.lease_ttl_s / 3
        renewed = tried = self._clock.monotonic()
        while (code := proc.poll()) is None:
            now = self._clock.monotonic()
            if now - tried >= every:
                tried = now
                held = self._renew(lease)
                if held:
                    renewed = now
                # Unreachable store: the lease is surely gone once its TTL passed unrenewed.
                if held is False or (held is None and now - renewed >= cfg.lease_ttl_s):
                    log.warning(
                        "pilot.lease_lost", pilot=self._pilot, item=lease.item,
                        action=lease.action_id,
                    )  # fmt: skip
                    self._terminate(proc)
                    return None
            self._clock.sleep(min(cfg.poll_s, every))
        return code

    def _renew(self, lease: QueueLease) -> bool | None:
        """True if renewed, False if the store says the lease is gone, None if it is
        unreachable (the caller keeps trying while the lease may still be live)."""
        cfg = self._cfg
        try:
            return self._store.queue_renew(
                cfg.domain, cfg.pool, lease.item, self._pilot, cfg.lease_ttl_s
            )
        except MetadataError as exc:
            log.warning("pilot.renew_failed", pilot=self._pilot, item=lease.item, error=str(exc))
            return None

    def _terminate(self, proc: Process) -> None:
        proc.send_signal(signal.SIGTERM)
        deadline = self._clock.monotonic() + self._cfg.kill_grace_s
        while proc.poll() is None:
            if self._clock.monotonic() >= deadline:
                proc.kill()
                break
            self._clock.sleep(min(self._cfg.poll_s, 0.1))

    def _finish(self, lease: QueueLease, code: int, reason: InfraReason | None) -> None:
        cfg = self._cfg
        try:
            recorded = self._store.queue_finish(
                cfg.domain,
                cfg.pool,
                lease.item,
                self._pilot,
                code,
                reason,
            )
        except MetadataError as exc:  # its lease expires: the action is retried
            log.error("pilot.report_failed", pilot=self._pilot, item=lease.item, error=str(exc))
            return
        if not recorded:
            log.warning("pilot.report_too_late", pilot=self._pilot, item=lease.item)


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def pilot_identity(environ: Mapping[str, str]) -> tuple[str, str | None]:
    """The pilot's name (its SLURM job id, `<array>_<task>` for an array task) and node."""
    node = environ.get("SLURMD_NODENAME") or socket.gethostname() or None
    array, task = environ.get("SLURM_ARRAY_JOB_ID"), environ.get("SLURM_ARRAY_TASK_ID")
    if array and task:
        return f"{array}_{task}", node
    job = environ.get("SLURM_JOB_ID")
    return (job or f"{node or 'host'}:{os.getpid()}"), node


# --- command line ------------------------------------------------------------------------------


class _UsageExit(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        sys.stderr.write(f"ebs-pilot: {message}\n")
        raise _UsageExit


def _license(text: str) -> tuple[str, int]:
    name, sep, count = text.partition("=")
    if not sep or not name or not count.isdigit() or int(count) < 1:
        raise argparse.ArgumentTypeError(f"expected NAME=COUNT, got {text!r}")
    return name, int(count)


def pilot_arguments(pilot_argv: Sequence[str], config: PilotConfig) -> list[str]:
    """The `ebs-pilot` command line for `config` (the inverse of `main`'s parser)."""
    alloc = config.allocation
    argv = [
        *pilot_argv,
        "--domain", config.domain,
        "--pool", str(config.pool),
        "--cpus", str(alloc.cpus),
        "--walltime-s", str(alloc.walltime_s),
        "--idle-s", repr(float(config.idle_s)),
        "--lease-ttl-s", str(config.lease_ttl_s),
        "--poll-s", repr(float(config.poll_s)),
        "--kill-grace-s", repr(float(config.kill_grace_s)),
        "--log-dir", str(config.log_dir),
    ]  # fmt: skip
    if alloc.mem_mb is not None:
        argv += ["--mem-mb", str(alloc.mem_mb)]
    for name, count in alloc.licenses:
        argv += ["--license", f"{name}={count}"]
    for feature in alloc.features:
        argv += ["--feature", feature]
    return [*argv, "--", *config.runner_argv]


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="ebs-pilot", description="Run queued actions of a pilot pool (P3-06).")
    p.add_argument("--domain", required=True)
    p.add_argument("--pool", required=True, type=UUID)
    p.add_argument("--cpus", required=True, type=int)
    p.add_argument("--mem-mb", type=int)
    p.add_argument("--license", action="append", type=_license, default=[])
    p.add_argument("--feature", action="append", default=[])
    p.add_argument("--walltime-s", required=True, type=int)
    p.add_argument("--idle-s", required=True, type=float)
    p.add_argument("--lease-ttl-s", required=True, type=int)
    p.add_argument("--poll-s", required=True, type=float)
    p.add_argument("--kill-grace-s", type=float, default=DEFAULT_KILL_GRACE_S)
    p.add_argument("--log-dir", required=True, type=Path)
    p.add_argument("runner", nargs="+", help="the ebs-runner command, after --")
    return p


def parse_pilot_arguments(argv: Sequence[str]) -> PilotConfig:
    """The inverse of `pilot_arguments` (without the command); `_UsageExit` on bad usage."""
    args = _parser().parse_args(argv)
    return PilotConfig(
        domain=args.domain,
        pool=args.pool,
        allocation=Allocation(
            cpus=args.cpus,
            mem_mb=args.mem_mb,
            licenses=tuple(sorted(dict(args.license).items())),
            features=tuple(sorted(set(args.feature))),
            walltime_s=args.walltime_s,
        ),
        runner_argv=tuple(args.runner),
        log_dir=args.log_dir,
        idle_s=args.idle_s,
        lease_ttl_s=args.lease_ttl_s,
        poll_s=args.poll_s,
        kill_grace_s=args.kill_grace_s,
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        config = parse_pilot_arguments(sys.argv[1:] if argv is None else argv)
    except _UsageExit:
        return EXIT_PILOT_USAGE
    environ = dict(os.environ)
    pilot, node = pilot_identity(environ)
    try:
        home = Path(environ.get("HOME") or Path.home())
        settings = load_config(config_paths(environ, cwd=Path.cwd(), home=home))
        store = open_metadata_store(settings.get("metadata", {}), environ=environ)
    except EbsError as exc:
        sys.stderr.write(f"ebs-pilot: {exc}\n")
        return EXIT_PILOT_ERROR
    worker = PilotWorker(store, config, pilot=pilot, node=node, env=environ)

    def on_term(signum: int, frame: FrameType | None) -> None:
        worker.stop()

    signal.signal(signal.SIGTERM, on_term)
    ran = worker.run()
    log.info("pilot.exit", pilot=pilot, ran=ran)
    return 0


if __name__ == "__main__":
    sys.exit(main())
