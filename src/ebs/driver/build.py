"""One build from plan to final status: the build record, events and the scheduler (P0-15 R10).

`run_build` stores the plan in the CAS, creates the build record (git info of the flow repo,
user, CI job, cache mode), adds the actions, runs the `Scheduler` and finishes the build with its
status and per-state counts. With `handle_sigint`, Ctrl-C cancels submitted work and records the
build as `cancelled` (R8).
"""

from __future__ import annotations

import signal
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from uuid import UUID, uuid4

from ebs.cas.api import CAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ExitCode
from ebs.core.log import get_logger
from ebs.driver.events import EventLog
from ebs.driver.retry import BackoffRetry, RetryPolicy
from ebs.driver.scheduler import DriverConfig, FinalState, Refiner, Scheduler
from ebs.exec.api import Executor
from ebs.meta.api import ActionRow, BuildCreate, BuildId, BuildStatus, MetadataStore
from ebs.plan.planfile import encode
from ebs.plan.types import Plan

_log = get_logger(__name__)

EXIT_CODES: dict[BuildStatus, ExitCode] = {
    "passed": ExitCode.OK,
    "failed": ExitCode.ACTIONS_FAILED,
    "infra_failed": ExitCode.INFRA,
    "cancelled": ExitCode.ACTIONS_FAILED,  # P0-16 may choose 130 for Ctrl-C
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


def events_path(workdir: Path, uuid: UUID) -> Path:
    return workdir / ".ebs" / "builds" / str(uuid) / "events.jsonl"


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
) -> BuildOutcome:
    config = config or DriverConfig()
    clock = clock or SystemClock()
    retry = retry or BackoffRetry(max_retries=config.max_retries)
    plan_digest = cas.put_bytes(encode(plan))
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
            cache_mode=config.cache_mode,
        )
    )
    store.add_actions(
        build, [ActionRow(action_id=a.action_id, step=a.step, key=a.key) for a in plan.actions]
    )
    path = events_path(workdir, uuid)
    events = EventLog(store, build, path, clock)
    events.emit("build_started", uuid=str(uuid), user=user, cache_mode=config.cache_mode)
    events.emit("plan_ready", plan=str(plan_digest), actions=len(plan.actions))
    scheduler = Scheduler(
        plan,
        plan_digest=plan_digest,
        refiner=refiner,
        cas=cas,
        store=store,
        executor=executor,
        build=build,
        events=events,
        clock=clock,
        config=config,
        retry=retry,
    )
    try:
        with _cancel_on_sigint(scheduler) if handle_sigint else nullcontext():
            states = scheduler.run()
    except BaseException as exc:
        scheduler.abort()
        interrupted = isinstance(exc, KeyboardInterrupt)
        _finish_quietly(store, build, "cancelled" if interrupted else "infra_failed")
        raise
    status = _status(scheduler)
    store.finish_build(build, status)
    counts = Counter(states.values())
    events.emit("build_finished", status=status, counts=dict(sorted(counts.items())))
    return BuildOutcome(build, uuid, status, EXIT_CODES[status], states, scheduler.plan, path)


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
def _cancel_on_sigint(scheduler: Scheduler) -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        _log.warning("driver.sigint_not_handled", reason="not in the main thread")
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        scheduler.request_cancel()
        signal.signal(signal.SIGINT, previous)  # a second Ctrl-C interrupts at once

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
