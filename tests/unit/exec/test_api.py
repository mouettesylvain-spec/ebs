from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from ebs.core.errors import ExecutorError
from ebs.exec.api import JobHandle, JobStatus, SubmitBatch
from tests.helpers.executor import PLAN, batch, spec
from tests.helpers.runner import DOMAIN


def test_batch_is_one_step_in_one_domain() -> None:
    assert batch("t[0]", "t[1]").step == "t"
    with pytest.raises(ExecutorError, match="empty"):
        SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=())
    with pytest.raises(ExecutorError, match="step"):
        SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=(spec("a"), spec("b")))
    other = dataclasses.replace(spec("t[1]"), domain="elsewhere")
    with pytest.raises(ExecutorError, match="domain"):
        SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=(spec("t[0]"), other))
    with pytest.raises(ExecutorError, match="twice"):
        SubmitBatch(plan=PLAN, domain=DOMAIN, build=None, actions=(spec("t[0]"), spec("t[0]")))


def test_handles_are_hashable_values() -> None:
    a = JobHandle(executor="local", job_id="1", action_id="t[0]")
    assert a == JobHandle(executor="local", job_id="1", action_id="t[0]")
    assert len({a, JobHandle(executor="local", job_id="2", action_id="t[0]")}) == 2


def test_status_reasons_belong_to_their_state() -> None:
    JobStatus(state="pending", pending_reason="resources")
    JobStatus(state="infra_failed", infra_reason="input_verification", exit_code=76)
    with pytest.raises(ValidationError):
        JobStatus(state="running", pending_reason="resources")
    with pytest.raises(ValidationError):
        JobStatus(state="done", infra_reason="oom")
    with pytest.raises(ValidationError):
        JobStatus(state="infra_failed")  # an infra failure always says why
    with pytest.raises(ValidationError):
        JobStatus(state="sleeping")  # type: ignore[arg-type]
