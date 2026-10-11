"""SLURM job state and pending reason ⇒ `JobStatus` (P1-01 R3).

SLURM decides the outcome when it killed the job (OOM, time limit, node failure, preemption,
scancel). When the batch script ended on its own (COMPLETED/FAILED), the script is `ebs-runner`
and its exit code is mapped like on every executor (`ebs.exec.api.status_for_exit`).
A job whose state carries the COMPLETING flag is still `running`: the epilog (scratch cleanup)
has not finished yet.
"""

from __future__ import annotations

from typing import Final

from ebs.core.log import get_logger
from ebs.exec.api import JobStatus, status_for_exit
from ebs.exec.slurm.models import SlurmJobInfo
from ebs.meta.api import InfraReason, PendingReason

__all__ = ["job_status", "pending_reason"]

log = get_logger(__name__)

_PENDING_STATES: Final = frozenset({"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED"})
_RUNNING_STATES: Final = frozenset(
    {"RUNNING", "CONFIGURING", "COMPLETING", "SUSPENDED", "STAGE_OUT", "RESIZING", "SIGNALING"}
)
_KILLED_BY_SLURM: Final[dict[str, InfraReason]] = {
    "OUT_OF_MEMORY": "oom",
    "TIMEOUT": "timeout",
    "DEADLINE": "timeout",
    "NODE_FAIL": "node_fail",
    "BOOT_FAIL": "node_fail",
    "PREEMPTED": "preempted",
}
_REASONS: Final[dict[str, PendingReason]] = {
    "Licenses": "licenses",
    "Resources": "resources",
    "ReqNodeNotAvail": "resources",
    "Priority": "priority",
}


def pending_reason(reason: str | None) -> PendingReason:
    """Map a SLURM pending reason ("Licenses", "ReqNodeNotAvail, UnavailableNodes:…")."""
    if not reason:
        return "other"
    head = reason.split(",", 1)[0].strip()
    return _REASONS.get(head, "other")


def _signed_exit(info: SlurmJobInfo) -> int | None:
    # JobStatus.exit_code is negative when a signal ended the runner.
    if info.signal:
        return -info.signal
    return info.exit_code


def job_status(info: SlurmJobInfo, *, infra_reason: InfraReason | None = None) -> JobStatus:
    """The executor-level status of a SLURM job.

    `infra_reason` is the runner's own reason, parsed by the caller from the runner's stderr
    (interfaces.md § 9); it only matters for runner exit 75.
    """
    state = info.state
    if state in _PENDING_STATES:
        return JobStatus(state="pending", pending_reason=pending_reason(info.reason))
    if state in _RUNNING_STATES or "COMPLETING" in info.flags:
        return JobStatus(state="running")
    if state in _KILLED_BY_SLURM:
        return JobStatus(
            state="infra_failed", infra_reason=_KILLED_BY_SLURM[state], exit_code=_signed_exit(info)
        )
    if state == "CANCELLED":
        return JobStatus(state="cancelled", exit_code=_signed_exit(info))
    if state in ("COMPLETED", "FAILED"):
        code = _signed_exit(info)
        if code is None:
            return JobStatus(state="infra_failed", infra_reason="runner_crash")
        return status_for_exit(code, infra_reason)
    log.warning("unknown SLURM job state", job_id=info.job_id, state=state)
    return JobStatus(state="infra_failed", infra_reason="other", exit_code=_signed_exit(info))
