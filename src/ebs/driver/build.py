"""One build from plan to final status: the build record, events and the scheduler (P0-15 R10).

`run_build` stores the plan in the CAS, creates the build record (git info of the flow repo,
user, CI job and its metadata, cache mode) with the releases it imports (`release_imports`,
P2-03), adds the actions, runs the `Scheduler` and finishes the build with its status and
per-state counts. With `handle_sigint`, Ctrl-C cancels submitted work and records the build as
`cancelled` (R8). The usage of the actions that ran and passed is appended to `.ebs/usage.jsonl`
for `ebs plan`'s tuning hints (P1-05 R4).
"""

from __future__ import annotations

import signal
import threading
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
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
from ebs.driver.resources_tuning import UsageLog, record_usage, usage_path
from ebs.driver.retry import BackoffRetry, RetryPolicy
from ebs.driver.scheduler import DriverConfig, FinalState, Refiner, Scheduler
from ebs.exec.api import Executor
from ebs.gc.access import put_registered
from ebs.gc.leases import LeaseKeeper
from ebs.meta.api import (
    ActionRow,
    BuildCreate,
    BuildId,
    BuildStatus,
    MetadataStore,
    ReleaseImport,
)
from ebs.plan.planfile import encode
from ebs.plan.types import ActionSpec, Plan

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


def events_path(workdir: Path, uuid: UUID) -> Path:
    return workdir / ".ebs" / "builds" / str(uuid) / "events.jsonl"


def action_row(spec: ActionSpec) -> ActionRow:
    """The store row of a planned action: with its rule kind, params and toolchain, which
    provenance queries show and select on (P2-04)."""
    params = {k: list(v) if isinstance(v, tuple) else v for k, v in spec.params.items()}
    return ActionRow(
        action_id=spec.action_id, step=spec.step, key=spec.key, rule=spec.rule.kind,
        params=params, toolchain=spec.toolchain.id if spec.toolchain else None,
    )  # fmt: skip


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
    ci: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    retry: RetryPolicy | None = None,
    handle_sigint: bool = False,
    release_imports: Sequence[ReleaseImport] = (),
) -> BuildOutcome:
    config = config or DriverConfig()
    clock = clock or SystemClock()
    retry = retry or BackoffRetry(max_retries=config.max_retries)
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
            cache_mode=config.cache_mode,
        )
    )
    if release_imports:  # for `ebs used-by` (P2-03 R5)
        store.record_release_imports(build, release_imports)
    store.add_actions(build, [action_row(a) for a in plan.actions])
    # GC roots for the build's lifetime (P1-09 R2): the plan and every input id known so far.
    lease = LeaseKeeper(store, plan.domain, build, clock)
    lease.start([plan_digest, *(r.id for a in plan.actions for r in a.inputs if r.id is not None)])
    path = events_path(workdir, uuid)
    events = EventLog(store, build, path, clock)
    started: dict[str, object] = {"uuid": str(uuid), "user": user, "cache_mode": config.cache_mode}
    if ci:  # P2-07 R1: the CI job's project, pipeline, job URL and commit
        started["ci"] = dict(sorted(ci.items()))
    events.emit("build_started", None, **started)
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
        lease=lease,
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
    record_usage(UsageLog(usage_path(workdir)), scheduler.usage)
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
