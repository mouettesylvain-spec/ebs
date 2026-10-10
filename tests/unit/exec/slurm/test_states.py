from __future__ import annotations

import pytest

from ebs.exec.api import JobStatus
from ebs.exec.slurm.models import JobId, SlurmJobInfo
from ebs.exec.slurm.states import job_status, pending_reason


def _info(
    state: str,
    *,
    reason: str | None = None,
    exit_code: int | None = None,
    signal: int | None = None,
    flags: frozenset[str] = frozenset(),
) -> SlurmJobInfo:
    return SlurmJobInfo(
        job_id=JobId("7"),
        state=state,
        flags=flags,
        reason=reason,
        exit_code=exit_code,
        signal=signal,
        source="squeue",
    )


# R3
@pytest.mark.parametrize(
    ("info", "expected"),
    [
        pytest.param(
            _info("PENDING", reason="Licenses"),
            JobStatus(state="pending", pending_reason="licenses"),
            id="pending-licenses",
        ),
        pytest.param(
            _info("PENDING", reason="Resources"),
            JobStatus(state="pending", pending_reason="resources"),
            id="pending-resources",
        ),
        pytest.param(
            _info("PENDING", reason="ReqNodeNotAvail, UnavailableNodes:n003"),
            JobStatus(state="pending", pending_reason="resources"),
            id="pending-reqnodenotavail",
        ),
        pytest.param(
            _info("PENDING", reason="Priority"),
            JobStatus(state="pending", pending_reason="priority"),
            id="pending-priority",
        ),
        pytest.param(
            _info("PENDING", reason="JobArrayTaskLimit"),
            JobStatus(state="pending", pending_reason="other"),
            id="pending-throttle-other",
        ),
        pytest.param(
            _info("PENDING", reason=None),
            JobStatus(state="pending", pending_reason="other"),
            id="pending-no-reason",
        ),
        pytest.param(
            _info("REQUEUED", reason="BeginTime"),
            JobStatus(state="pending", pending_reason="other"),
            id="requeued-is-pending",
        ),
        pytest.param(_info("RUNNING"), JobStatus(state="running"), id="running"),
        pytest.param(_info("CONFIGURING"), JobStatus(state="running"), id="configuring"),
        pytest.param(_info("SUSPENDED"), JobStatus(state="running"), id="suspended"),
        pytest.param(
            _info("COMPLETED", exit_code=0, flags=frozenset({"COMPLETING"})),
            JobStatus(state="running"),
            id="completing-flag-still-running",
        ),
        pytest.param(
            _info("OUT_OF_MEMORY", exit_code=0),
            JobStatus(state="infra_failed", infra_reason="oom", exit_code=0),
            id="oom",
        ),
        pytest.param(
            _info("TIMEOUT", exit_code=0, signal=15),
            JobStatus(state="infra_failed", infra_reason="timeout", exit_code=-15),
            id="timeout",
        ),
        pytest.param(
            _info("DEADLINE"),
            JobStatus(state="infra_failed", infra_reason="timeout"),
            id="deadline",
        ),
        pytest.param(
            _info("NODE_FAIL", exit_code=0),
            JobStatus(state="infra_failed", infra_reason="node_fail", exit_code=0),
            id="node-fail",
        ),
        pytest.param(
            _info("BOOT_FAIL"),
            JobStatus(state="infra_failed", infra_reason="node_fail"),
            id="boot-fail",
        ),
        pytest.param(
            _info("PREEMPTED", exit_code=0, signal=15),
            JobStatus(state="infra_failed", infra_reason="preempted", exit_code=-15),
            id="preempted",
        ),
        pytest.param(
            _info("CANCELLED", exit_code=0, signal=15),
            JobStatus(state="cancelled", exit_code=-15),
            id="cancelled",
        ),
        pytest.param(
            _info("COMPLETED", exit_code=0),
            JobStatus(state="done", exit_code=0),
            id="completed-runner-0",
        ),
        pytest.param(
            _info("FAILED", exit_code=75),
            JobStatus(state="infra_failed", infra_reason="other", exit_code=75),
            id="failed-runner-75",
        ),
        pytest.param(
            _info("FAILED", exit_code=76),
            JobStatus(state="infra_failed", infra_reason="input_verification", exit_code=76),
            id="failed-runner-76",
        ),
        pytest.param(
            _info("FAILED", exit_code=1),
            JobStatus(state="infra_failed", infra_reason="runner_crash", exit_code=1),
            id="failed-runner-1",
        ),
        pytest.param(
            _info("FAILED", exit_code=0, signal=9),
            JobStatus(state="infra_failed", infra_reason="runner_crash", exit_code=-9),
            id="failed-by-signal",
        ),
        pytest.param(
            _info("FAILED"),
            JobStatus(state="infra_failed", infra_reason="runner_crash"),
            id="failed-without-exit-code",
        ),
    ],
)
def test_mapping(info: SlurmJobInfo, expected: JobStatus) -> None:
    assert job_status(info) == expected


# R3: exit 75 carries the runner's own reason when the caller read it from stderr.
def test_runner_infra_reason_is_passed_through() -> None:
    status = job_status(_info("FAILED", exit_code=75), infra_reason="license")
    assert status == JobStatus(state="infra_failed", infra_reason="license", exit_code=75)


# R3: SLURM's own verdict wins over a runner reason (an OOM kill is an OOM).
def test_slurm_terminal_state_wins_over_runner_reason() -> None:
    status = job_status(_info("OUT_OF_MEMORY", exit_code=0), infra_reason="license")
    assert status.infra_reason == "oom"


# R3
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("Licenses", "licenses"),
        ("Resources", "resources"),
        ("ReqNodeNotAvail", "resources"),
        ("ReqNodeNotAvail, Reserved for maintenance", "resources"),
        ("Priority", "priority"),
        ("Dependency", "other"),
        ("QOSMaxJobsPerUserLimit", "other"),
        ("None", "other"),
        ("", "other"),
    ],
)
def test_pending_reason(reason: str, expected: str) -> None:
    assert pending_reason(reason) == expected


# R3: an unknown state is never silently "done".
def test_unknown_state_is_reported_as_other_infra_failure() -> None:
    status = job_status(_info("SOMETHING_NEW"))
    assert status == JobStatus(state="infra_failed", infra_reason="other")
