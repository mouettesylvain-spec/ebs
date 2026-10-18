"""Resuming a running build with a new driver (P2-08 R2): after a crash, or in a detached job.

`resume_build` reloads the build's plan from the CAS and, once it holds the build's driver lease
(R3), reconciles the action states recorded in the MetadataStore with the executor:

- final states (`done`, `failed`, `cached`, `skipped`, and `infra_failed` with a `finished`
  event) stay; the recorded results of successes refine the plan again, so consumers get keys;
- `pending`/`running` actions with a recorded job id are re-attached (`Reattaching.adopt`) and
  polled like jobs submitted here: nothing that runs, or already ran, is resubmitted;
- `pending`/`running` actions whose job cannot be re-attached (no job id, or an executor that
  cannot adopt) are lost with the old driver: they go back to `queued` through `infra_failed`
  and run again;
- `infra_failed` actions without a `finished` event were waiting for a retry: back to `queued`;
- `queued` actions are scheduled as usual.

The old driver's in-memory retry state (attempt counts, raised memory, avoided nodes) is not
recorded and starts afresh. The build's own cache mode applies, whatever `config` says.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ebs.cas.api import CAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.digest import Digest
from ebs.core.errors import DriverBusyError, MetadataError
from ebs.core.log import get_logger
from ebs.driver.build import BuildOutcome, StartedBuild, drive_build, events_path
from ebs.driver.events import EventLog, read_events
from ebs.driver.retry import RetryPolicy
from ebs.driver.scheduler import DriverConfig, Refiner, Scheduler
from ebs.exec.api import Executor, JobHandle
from ebs.meta.api import (
    TERMINAL_STATES,
    ActionRow,
    ActionState,
    BuildId,
    MetadataStore,
    ResultManifest,
)
from ebs.plan.planfile import decode
from ebs.plan.planner import refine_plan
from ebs.plan.types import Plan

__all__ = ["Reattaching", "Reconciliation", "reconcile", "resume_build"]

_log = get_logger(__name__)

_WITH_RESULT = frozenset({"done", "failed", "cached"})


@runtime_checkable
class Reattaching(Protocol):
    """An executor whose jobs outlive the driver (SLURM): it can track them again."""

    name: str

    def adopt(self, build: BuildId, handles: Sequence[JobHandle]) -> None: ...


@dataclass(frozen=True)
class Reconciliation:
    """What a resumed driver does with each action (see the module docstring)."""

    states: dict[str, ActionState]  # the store state once reconciled
    attach: dict[str, str]  # action -> job id to re-attach
    rerun: tuple[str, ...]  # lost with the old driver: run again
    retry: tuple[str, ...]  # infrastructure failures that were waiting for a retry


def reconcile(
    rows: Iterable[ActionRow], *, finished: AbstractSet[str], can_attach: bool
) -> Reconciliation:
    """Decide per action from its stored row; `finished` = actions with a `finished` event."""
    states: dict[str, ActionState] = {}
    attach: dict[str, str] = {}
    rerun: list[str] = []
    retry: list[str] = []
    for row in rows:
        action_id, state = row.action_id, row.state
        if state in ("pending", "running"):
            if can_attach and row.slurm_job_id is not None:
                attach[action_id] = row.slurm_job_id
            else:
                rerun.append(action_id)
                state = "queued"
        elif state == "infra_failed" and action_id not in finished:
            retry.append(action_id)
            state = "queued"
        states[action_id] = state
    return Reconciliation(states, attach, tuple(rerun), tuple(retry))


class _PlanRefiner:
    def refine(self, plan: Plan, produced: Mapping[tuple[str, str], Digest]) -> Plan:
        return refine_plan(plan, produced)


def resume_build(
    build: BuildId,
    *,
    cas: CAS,
    store: MetadataStore,
    executor: Executor,
    workdir: Path,
    config: DriverConfig | None = None,
    clock: Clock | None = None,
    retry: RetryPolicy | None = None,
    owner: str | None = None,
    refiner: Refiner | None = None,
    handle_sigint: bool = False,
    handle_sigterm: bool = False,
) -> BuildOutcome:
    """Drive the running build `build` to its end from where its last driver left it.

    DriverBusyError if the build is finished or another driver holds its lease.
    """
    clock = clock or SystemClock()
    view = store.get_build(build)
    if view.status != "running":
        raise DriverBusyError(
            f"build {build} ({view.uuid}) is already finished ({view.status}); there is "
            "nothing to resume: start a new build"
        )
    if view.domain != cas.domain:
        raise MetadataError(
            f"build {build} is in domain {view.domain!r}, but the CAS given is for {cas.domain!r}"
        )
    with cas.open(view.plan_digest) as f:
        plan = decode(f.read())
    if not isinstance(executor, Reattaching):
        # Their jobs would keep running untracked while the actions ran again here.
        orphans = [
            r.action_id
            for r in store.list_actions(build)
            if r.state in ("pending", "running") and r.slurm_job_id is not None
        ]
        if orphans:
            raise DriverBusyError(
                f"build {build} has {len(orphans)} job(s) submitted to SLURM (e.g. "
                f"{orphans[0]}) that executor {executor.name!r} cannot re-attach to; resume "
                "with `--executor slurm`"
            )
    config = dataclasses.replace(config or DriverConfig(), cache_mode=view.cache_mode)
    started = StartedBuild(build, view.uuid, view.plan_digest)

    def restore(scheduler: Scheduler, events: EventLog) -> None:
        # Runs once this driver holds the lease: nobody else changes these rows meanwhile.
        rec = reconcile(
            store.list_actions(build),
            finished=_finished(workdir, started),
            can_attach=isinstance(executor, Reattaching),
        )
        for action_id in rec.rerun:  # its job is unknown: the attempt failed with the driver
            store.set_action_state(build, action_id, "infra_failed", infra_reason="other")
            store.set_action_state(build, action_id, "queued")
        for action_id in rec.retry:
            store.set_action_state(build, action_id, "queued")
        results: dict[str, ResultManifest] = {}
        for action_id, state in rec.states.items():
            if state in _WITH_RESULT:
                result = store.get_result(build, action_id)
                if result is None:
                    raise MetadataError(
                        f"build {build}: action {action_id} is {state} but has no recorded "
                        "result; the build record is inconsistent, start a new build"
                    )
                results[action_id] = result
        handles = [JobHandle(executor.name, job, a) for a, job in sorted(rec.attach.items())]
        if handles:
            assert isinstance(executor, Reattaching)  # attach is empty otherwise
            executor.adopt(build, handles)
        scheduler.restore(rec.states, results, handles)
        final = sum(1 for s in rec.states.values() if s in TERMINAL_STATES)
        events.emit(
            "build_resumed",
            attached=len(handles),
            rerun=len(rec.rerun) + len(rec.retry),
            finished=final,
        )
        _log.info(
            "driver.resumed",
            build=build,
            attached=len(handles),
            rerun=len(rec.rerun),
            retry=len(rec.retry),
            finished=final,
        )

    return drive_build(
        plan,
        started,
        refiner=refiner or _PlanRefiner(),
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
        restore=restore,
    )


def _finished(workdir: Path, started: StartedBuild) -> set[str]:
    """Actions with a `finished` event in the local event log (none if it is not here)."""
    path = events_path(workdir, started.uuid)
    if not path.exists():
        return set()
    return {
        e.action_id for e in read_events(path) if e.type == "finished" and e.action_id is not None
    }
