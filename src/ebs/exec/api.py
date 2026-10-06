"""The executor contract the driver talks to (docs/design/interfaces.md § 8).

An executor runs `ebs-runner` for the actions of a `SubmitBatch` somewhere (local processes, SLURM
jobs) and reports a `JobStatus` per `JobHandle`. It never reads results: `done` only means the
runner finished and posted (or printed) a ResultManifest; the driver reads it from MetadataStore.

Runner exit codes map to states the same way on every executor (`status_for_exit`).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from ebs.core.digest import Digest
from ebs.core.errors import ExecutorError
from ebs.meta.api import BuildId, InfraReason, PendingReason
from ebs.plan.types import ActionSpec
from ebs.runner.errors import EXIT_INFRA, EXIT_OK, EXIT_VERIFY

__all__ = [
    "TERMINAL_JOB_STATES",
    "Executor",
    "JobHandle",
    "JobState",
    "JobStatus",
    "SubmitBatch",
    "status_for_exit",
]

JobState = Literal["pending", "running", "done", "infra_failed", "cancelled"]

TERMINAL_JOB_STATES: Final[frozenset[str]] = frozenset({"done", "infra_failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class JobHandle:
    """One submitted action. A hashable value: the driver keys its in-flight table by it."""

    executor: str  # Executor.name of the executor that handed it out
    job_id: str  # executor-specific ("17" locally, "1234_5" for a SLURM array element)
    action_id: str


@dataclass(frozen=True)
class SubmitBatch:
    """Actions of one step and one domain, with compatible resources, submitted together.

    The driver groups them (P0-15 R7) so that a SLURM executor can make one job array of a batch.
    """

    plan: Digest  # plan.json in the domain's CAS; the runner loads the actions from it
    domain: str
    build: BuildId | None  # None: the runner posts nothing and prints the result
    actions: tuple[ActionSpec, ...]

    def __post_init__(self) -> None:
        if not self.actions:
            raise ExecutorError(
                "cannot submit an empty batch: the driver sends at least one action"
            )
        steps = {a.step for a in self.actions}
        if len(steps) != 1:
            raise ExecutorError(
                f"a batch holds actions of one step, got {sorted(steps)}: split it per step"
            )
        domains = {a.domain for a in self.actions} - {self.domain}
        if domains:
            raise ExecutorError(
                f"batch for domain {self.domain!r} contains actions of domain(s) {sorted(domains)}"
            )
        ids = [a.action_id for a in self.actions]
        if len(set(ids)) != len(ids):
            dup = sorted({i for i in ids if ids.count(i) > 1})
            raise ExecutorError(f"action(s) {dup} appear twice in one batch")

    @property
    def step(self) -> str:
        return self.actions[0].step


class JobStatus(BaseModel):
    """Where a job stands. Reasons are set exactly for the state they explain."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    state: JobState
    pending_reason: PendingReason | None = None
    infra_reason: InfraReason | None = None
    exit_code: int | None = None  # the runner's; negative = killed by that signal

    @model_validator(mode="after")
    def _reasons_match_state(self) -> JobStatus:
        if self.pending_reason is not None and self.state != "pending":
            raise ValueError("pending_reason is only set for state 'pending'")
        if (self.infra_reason is not None) != (self.state == "infra_failed"):
            raise ValueError("infra_reason is set exactly for state 'infra_failed'")
        return self


class Executor(Protocol):
    name: str

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        """Start or queue every action of `batch`; one handle per action, in batch order."""
        ...

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        """The status of every handle, without blocking; ONE backend call per poll."""
        ...

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        """Stop the jobs (the runner cleans its scratch); they report `cancelled` from now on.

        Jobs that already finished keep their state.
        """
        ...


def status_for_exit(exit_code: int, infra_reason: InfraReason | None = None) -> JobStatus:
    """Map a runner exit code (interfaces.md § 9) to a final JobStatus.

    0 ⇒ done; 75 ⇒ infra_failed with the runner's reason (default "other"); 76 ⇒
    infra_failed("input_verification"); anything else, including death by a signal (negative
    code), means the runner itself broke ⇒ infra_failed("runner_crash").
    """
    if exit_code == EXIT_OK:
        return JobStatus(state="done", exit_code=exit_code)
    if exit_code == EXIT_INFRA:
        reason: InfraReason = infra_reason or "other"
    elif exit_code == EXIT_VERIFY:
        reason = "input_verification"
    else:
        reason = "runner_crash"
    return JobStatus(state="infra_failed", infra_reason=reason, exit_code=exit_code)
