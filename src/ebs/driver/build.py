"""One build from plan to final status: the build record, events and the scheduler (P0-15 R10).

`run_build` = `start_build` (stores the plan in the CAS, creates the build record with the git
info of the flow repo, user, CI job and cache mode, adds the actions) + `drive_build` (claims the
build's driver lease, runs the `Scheduler` and finishes the build with its status and per-state
counts). `ebs build --detach` starts a build here and drives it in a SLURM job; a resumed driver
(`ebs.driver.resume`, P2-08) drives a build whose driver died. With `handle_sigint` /
`handle_sigterm`, Ctrl-C / SIGTERM (`scancel` of a driver job) cancels submitted work and records
the build as `cancelled` (R8; P2-08 R4). The usage of the actions that ran and passed is appended
to `.ebs/usage.jsonl` for `ebs plan`'s tuning hints (P1-05 R4).
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, TypeAlias
from uuid import UUID, uuid4

from ebs.cas.api import CAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import DriverBusyError, ExitCode, MetadataError
from ebs.core.log import get_logger
from ebs.driver.events import EventLog
from ebs.driver.resources_tuning import UsageLog, record_usage, usage_path
from ebs.driver.retry import BackoffRetry, RetryPolicy
from ebs.driver.scheduler import DriverConfig, FinalState, Refiner, Scheduler
from ebs.exec.api import Executor
from ebs.gc.access import put_registered
from ebs.gc.leases import LeaseKeeper
from ebs.meta.api import (
    DRIVER_LEASE_TTL_S,
    ActionRow,
    BuildCreate,
    BuildId,
    BuildStatus,
    CacheMode,
    MetadataStore,
)
from ebs.plan.planfile import encode
from ebs.plan.types import Plan

_log = get_logger(__name__)

EXIT_CODES: dict[BuildStatus, ExitCode] = {
    "passed": ExitCode.OK,
    "failed": ExitCode.ACTIONS_FAILED,
    "infra_failed": ExitCode.INFRA,
    "cancelled": ExitCode.CANCELLED,  # 130, like a shell after Ctrl-C
}


@dataclass(frozen=True)
class BuildOutcome:
    build: BuildId
    uuid: UUID
    status: BuildStatus
    exit_code: ExitCode
    states: dict[str, FinalState]  # plan order
    plan: Plan  # as refined: every action that could be keyed has its key
    events_path: Path


@dataclass(frozen=True)
class StartedBuild:
    """A build record with its actions, whose plan is in the CAS (`start_build`)."""

    build: BuildId
    uuid: UUID
    plan_digest: Digest


def events_path(workdir: Path, uuid: UUID) -> Path:
    return workdir / ".ebs" / "builds" / str(uuid) / "events.jsonl"


def driver_owner() -> str:
    """This process as a driver lease owner: `host:pid:token` (the token tells apart two
    drivers that happen to share a host name and pid)."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


class DriverLeaseKeeper:
    """Holds the build's driver lease (P2-08 R3): one live driver per build.

    `claim` refuses (DriverBusyError) while another driver holds a live lease. `heartbeat`
    (each scheduler pass) renews it every third of its TTL; if another driver took it over
    meanwhile (this one stalled past the TTL), it raises DriverBusyError and this driver stops,
    leaving the jobs to the new one. A renewal the store cannot answer is only logged: the
    next heartbeat tries again.
    """

    def __init__(
        self,
        store: MetadataStore,
        build: BuildId,
        owner: str,
        clock: Clock,
        *,
        ttl_s: int = DRIVER_LEASE_TTL_S,
    ) -> None:
        self._store = store
        self._build = build
        self.owner = owner
        self._clock = clock
        self._ttl_s = ttl_s
        self._renewed_at = 0.0

    def claim(self) -> None:
        lease = self._store.claim_driver(self._build, self.owner, self._ttl_s)
        if lease.owner != self.owner:
            raise DriverBusyError(
                f"build {self._build} already has a live driver, {lease.owner} (its lease "
                f"expires {lease.expires_at.isoformat(timespec='seconds')} unless renewed); "
                "follow it with `ebs status`, or resume once that driver is gone and its lease "
                "has expired"
            )
        self._renewed_at = self._clock.monotonic()

    def heartbeat(self) -> None:
        if self._clock.monotonic() - self._renewed_at < self._ttl_s / 3:
            return
        try:
            lease = self._store.claim_driver(self._build, self.owner, self._ttl_s)
        except MetadataError as exc:
            _log.warning("driver.lease_renew_failed", build=self._build, error=str(exc))
            return
        if lease.owner != self.owner:
            raise DriverBusyError(
                f"build {self._build} was taken over by driver {lease.owner} while this driver "
                f"({self.owner}) stalled past its lease; this driver stops and leaves the "
                "build's jobs to the new one"
            )
        self._renewed_at = self._clock.monotonic()

    def release(self) -> None:
        try:
            self._store.release_driver(self._build, self.owner)
        except MetadataError as exc:  # it expires on its own
            _log.warning("driver.lease_release_failed", build=self._build, error=str(exc))


def start_build(
    plan: Plan,
    *,
    cas: CAS,
    store: MetadataStore,
    workdir: Path,
    user: str,
    cache_mode: CacheMode = "read",
    ci_job: str | None = None,
    clock: Clock | None = None,
) -> StartedBuild:
    """Store the plan, create the build record and its (queued) actions, and emit
    `build_started` and `plan_ready`. No driver runs it yet: `drive_build` does, or a driver
    resumed into it (P2-08)."""
    clock = clock or SystemClock()
    plan_digest = put_registered(cas, store, plan.domain, encode(plan))
    uuid = uuid4()
    git = plan.flow.git
    build = store.create_build(
        BuildCreate(
            uuid=uuid,
            domain=plan.domain,
            project=plan.project,
            plan_digest=plan_digest,
            flow_repo=git.repo if git else "",
            flow_commit=git.commit if git else "",
            flow_dirty=git.dirty if git else False,
            user_name=user,
            ci_job=ci_job,
            cache_mode=cache_mode,
        )
    )
    store.add_actions(
        build, [ActionRow(action_id=a.action_id, step=a.step, key=a.key) for a in plan.actions]
    )
    events = EventLog(store, build, events_path(workdir, uuid), clock)
    events.emit("build_started", uuid=str(uuid), user=user, cache_mode=cache_mode)
    events.emit("plan_ready", plan=str(plan_digest), actions=len(plan.actions))
    return StartedBuild(build, uuid, plan_digest)


def run_build(
    plan: Plan,
    *,
    refiner: Refiner,
    cas: CAS,
    store: MetadataStore,
    executor: Executor,
    workdir: Path,
    user: str,
    config: DriverConfig | None = None,
    ci_job: str | None = None,
    clock: Clock | None = None,
    retry: RetryPolicy | None = None,
    handle_sigint: bool = False,
    handle_sigterm: bool = False,
    owner: str | None = None,
) -> BuildOutcome:
    config = config or DriverConfig()
    clock = clock or SystemClock()
    started = start_build(
        plan,
        cas=cas,
        store=store,
        workdir=workdir,
        user=user,
        cache_mode=config.cache_mode,
        ci_job=ci_job,
        clock=clock,
    )
    return drive_build(
        plan,
        started,
        refiner=refiner,
        cas=cas,
        store=store,
        executor=executor,
        workdir=workdir,
        config=config,
        clock=clock,
        retry=retry,
        handle_sigint=handle_sigint,
        handle_sigterm=handle_sigterm,
        owner=owner,
    )


Restore: TypeAlias = Callable[[Scheduler, EventLog], None]
"""Prepares the scheduler of a resumed build (P2-08), once this driver holds the lease."""


def drive_build(
    plan: Plan,
    started: StartedBuild,
    *,
    refiner: Refiner,
    cas: CAS,
    store: MetadataStore,
    executor: Executor,
    workdir: Path,
    config: DriverConfig | None = None,
    clock: Clock | None = None,
    retry: RetryPolicy | None = None,
    handle_sigint: bool = False,
    handle_sigterm: bool = False,
    owner: str | None = None,
    restore: Restore | None = None,
) -> BuildOutcome:
    """Drive a started build to its final status as its only driver: DriverBusyError if
    another driver holds the build's lease, or takes it over meanwhile (then the build and its
    jobs are left to that driver)."""
    config = config or DriverConfig()
    clock = clock or SystemClock()
    retry = retry or BackoffRetry(max_retries=config.max_retries)
    build, uuid = started.build, started.uuid
    driver = DriverLeaseKeeper(store, build, owner or driver_owner(), clock)
    driver.claim()
    try:
        # GC roots for the build's lifetime (P1-09 R2): the plan and every input id known so far.
        lease = LeaseKeeper(store, plan.domain, build, clock)
        inputs = [r.id for a in plan.actions for r in a.inputs if r.id is not None]
        lease.start([started.plan_digest, *inputs])
        path = events_path(workdir, uuid)
        events = EventLog(store, build, path, clock)
        scheduler = Scheduler(
            plan,
            plan_digest=started.plan_digest,
            refiner=refiner,
            cas=cas,
            store=store,
            executor=executor,
            build=build,
            events=events,
            clock=clock,
            config=config,
            retry=retry,
            lease=lease,
            driver_lease=driver,
        )
        signals = [
            sig
            for sig, wanted in ((signal.SIGINT, handle_sigint), (signal.SIGTERM, handle_sigterm))
            if wanted
        ]
        try:
            if restore is not None:
                restore(scheduler, events)
            with _cancel_on_signals(scheduler, signals):
                states = scheduler.run()
        except DriverBusyError:
            raise  # another driver took the build over: the jobs and the record are its own
        except BaseException as exc:
            scheduler.abort()
            interrupted = isinstance(exc, KeyboardInterrupt)
            _finish_quietly(store, build, "cancelled" if interrupted else "infra_failed")
            raise
        status = _status(scheduler)
        store.finish_build(build, status)
        counts = Counter(states.values())
        events.emit("build_finished", status=status, counts=dict(sorted(counts.items())))
        record_usage(UsageLog(usage_path(workdir)), scheduler.usage)
        return BuildOutcome(build, uuid, status, EXIT_CODES[status], states, scheduler.plan, path)
    finally:
        driver.release()


def _status(scheduler: Scheduler) -> BuildStatus:
    if scheduler.cancelled:
        return "cancelled"
    if scheduler.infra_exhausted:  # exit 3 only when retries were really used up
        return "infra_failed"
    if scheduler.failures:
        return "failed"
    return "passed"


def _finish_quietly(store: MetadataStore, build: BuildId, status: BuildStatus) -> None:
    try:
        store.finish_build(build, status)
    except Exception:
        _log.warning("driver.finish_after_error_failed", build=build)


@contextmanager
def _cancel_on_signals(scheduler: Scheduler, signals: Sequence[signal.Signals]) -> Iterator[None]:
    """Each of `signals` asks the scheduler to cancel; a second one of the same kind acts at
    once (its previous handler is back)."""
    if not signals:
        yield
        return
    if threading.current_thread() is not threading.main_thread():
        _log.warning("driver.signals_not_handled", reason="not in the main thread")
        yield
        return
    previous: dict[int, Any] = {}

    def handler(signum: int, frame: FrameType | None) -> None:
        del frame
        scheduler.request_cancel()
        signal.signal(signum, previous[signum])

    for sig in signals:
        previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)
