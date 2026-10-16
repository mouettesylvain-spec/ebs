"""Jobs wait for licenses in SLURM, and `ebs status` says so (P1-03 R1, R3, R6).

FakeSlurm holds one `simfeat@flexlm` license: of two actions asking for it, one runs and the
other stays PENDING with reason `Licenses`, which the executor reports as `pending: licenses`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest

from ebs.cas.fs import FsCAS
from ebs.cli._records import find_build
from ebs.cli.status import status_document
from ebs.core.errors import ExecutorError
from ebs.exec.api import JobStatus, SubmitBatch
from ebs.exec.slurm.accounts import QosSettings
from ebs.exec.slurm.cli import SlurmCli
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.plan.types import ActionSpec
from tests.fakes.slurm import FakeSlurm
from tests.helpers.cli import write_build
from tests.helpers.executor import PLAN, FakeRunner, spec, wait_states
from tests.helpers.runner import DOMAIN
from tests.helpers.wait import wait_until

U1 = UUID("33333333-3333-4333-8333-333333333333")


def _executor(tmp_path: Path, fake: FakeSlurm, runner: FakeRunner) -> SlurmExecutor:
    runner.cas_root = tmp_path / "cas"
    return SlurmExecutor(
        cli=SlurmCli(bin_dir=fake.bin_dir),
        cas=FsCAS(runner.cas_root, DOMAIN),
        settings=SlurmSettings(
            debug_root=tmp_path / "debug",
            account_map={"rv32x-cpu": "cpu-team"},
            qos=QosSettings(default="normal", ci="ci-high"),
        ),
        runner_argv=runner.argv,
        env=runner.env(),
        project="rv32x-cpu",
        build_kind="ci",
    )


def _with_licenses(action_id: str, licenses: dict[str, int]) -> ActionSpec:
    return replace(spec(action_id), licenses=licenses)


# R6 (with R1, R3: the request and the account/QOS reach SLURM and SLURM accepts them)
def test_pending_licenses(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(
        cpus=8, licenses={"simfeat@flexlm": 1}, accounts=["cpu-team"], qos=["normal", "ci-high"]
    )
    runner = FakeRunner(tmp_path / "runner")
    for a in ("sim[a]", "sim[b]"):
        runner.set(a, hold=True)
    ex = _executor(tmp_path, fake_slurm, runner)
    handles = [
        *ex.submit(SubmitBatch(PLAN, DOMAIN, None, (_with_licenses("sim[a]", {"simfeat": 1}),))),
        *ex.submit(SubmitBatch(PLAN, DOMAIN, None, (_with_licenses("sim[b]", {"simfeat": 1}),))),
    ]
    wait_until(lambda: len(runner.started()) == 1, what="one job holds the license")
    states = wait_states(ex, handles, until={"running", "pending"})
    by_action = {h.action_id: s for h, s in states.items()}
    (running,) = runner.started()
    waiting = "sim[b]" if running == "sim[a]" else "sim[a]"
    assert by_action[running].state == "running"
    assert by_action[waiting] == JobStatus(state="pending", pending_reason="licenses")

    # `ebs status` shows the reason the driver records for that status (P0-16 R3).
    events = [
        ("pending", h.action_id, {"reason": s.pending_reason})
        if s.state == "pending"
        else (s.state, h.action_id, {"job_id": h.job_id})
        for h, s in states.items()
    ]
    write_build(tmp_path, U1, {"sim[a]": "sim", "sim[b]": "sim"}, events)
    record = find_build(tmp_path, None)
    doc = status_document(record, record.events(tmp_path))
    assert doc["totals"] == {"pending: licenses": 1, "running": 1}

    # Releasing the first job frees the license: the second starts and both finish.
    runner.release(running)
    wait_until(lambda: len(runner.started()) == 2, what="the waiting job got the license")
    runner.release(waiting)
    final = wait_states(ex, handles, timeout=30)
    assert all(s.state == "done" for s in final.values()), final


# R2: an unknown feature is rejected by SLURM; the error names it.
def test_unknown_feature_rejected(tmp_path: Path, fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(licenses={"simfeat@flexlm": 1})
    runner = FakeRunner(tmp_path / "runner")
    ex = _executor(tmp_path, fake_slurm, runner)
    with pytest.raises(ExecutorError) as info:
        ex.submit(SubmitBatch(PLAN, DOMAIN, None, (_with_licenses("x", {"nosuchfeat": 1}),)))
    assert "nosuchfeat@flexlm" in str(info.value)
    assert "Invalid license specification" in str(info.value)
