"""The node a job ran on, and nodes excluded from a build (P1-05 R3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import ExecutorError
from ebs.exec.api import JobStatus, SubmitBatch
from ebs.exec.slurm.cli import SbatchOptions, sbatch_argv
from ebs.exec.slurm.executor import SlurmExecutor, SlurmSettings
from ebs.exec.slurm.models import SacctJob, SlurmJobInfo, SqueueJob
from ebs.exec.slurm.states import job_status
from tests.helpers.executor import PLAN, batch, spec
from tests.helpers.runner import DOMAIN
from tests.helpers.slurm import ScriptedSlurm


def _executor(tmp_path: Path, slurm: ScriptedSlurm, cas: FsCAS) -> SlurmExecutor:
    return SlurmExecutor(
        cli=slurm.cli(),
        cas=cas,
        settings=SlurmSettings(debug_root=tmp_path / "debug"),
        runner_argv=("/opt/ebs/bin/ebs-runner",),
        env={"PATH": "/usr/bin:/bin"},
        clock=FakeClock(),
    )


def _opt(argv: list[str], name: str) -> str | None:
    prefix = f"--{name}="
    return next((a[len(prefix) :] for a in argv if a.startswith(prefix)), None)


# R3: excluded nodes reach sbatch as one --exclude list; none, no option.
def test_exclude_on_sbatch_line(tmp_path: Path, cas: FsCAS) -> None:
    slurm = ScriptedSlurm()
    ex = _executor(tmp_path, slurm, cas)
    ex.submit(SubmitBatch(PLAN, DOMAIN, None, (spec("t[0]"),), exclude_nodes=("n2", "n10")))
    ex.submit(batch("plain"))
    excluded, plain = slurm.calls
    assert _opt(excluded, "exclude") == "n10,n2"
    assert _opt(plain, "exclude") is None
    opts = SbatchOptions(job_name="j", exclude=("b", "a"))
    assert "--exclude=a,b" in sbatch_argv(Path("/s.sh"), opts)
    assert not any(
        a.startswith("--exclude") for a in sbatch_argv(Path("/s.sh"), SbatchOptions(job_name="j"))
    )


# R3
@pytest.mark.parametrize("bad", [("",), ("n1,n2",), ("n 1",), ("-n1",), ("n[1-2]",)])
def test_exclude_validation(bad: tuple[str, ...]) -> None:
    with pytest.raises(ExecutorError, match="node"):
        SubmitBatch(PLAN, DOMAIN, None, (spec("t[0]"),), exclude_nodes=bad)
    with pytest.raises(ValueError, match="exclude"):
        SbatchOptions(job_name="j", exclude=bad)


# R3: the node comes back with the status (squeue while known, sacct after).
def test_node_reported(tmp_path: Path, cas: FsCAS) -> None:
    slurm = ScriptedSlurm()
    ex = _executor(tmp_path, slurm, cas)
    (h,) = ex.submit(batch("t[0]"))
    assert ex.poll([h])[h] == JobStatus(state="pending", pending_reason="priority")
    slurm.set_state("100", "NODE_FAIL", node="node-07")
    status = ex.poll([h])[h]
    assert (status.state, status.infra_reason, status.node) == (
        "infra_failed",
        "node_fail",
        "node-07",
    )


# R3
@pytest.mark.parametrize(
    ("raw", "node"),
    [
        ("node-07", "node-07"),
        ("n001.cluster", "n001.cluster"),
        ("", None),
        ("None assigned", None),
        ("None", None),
        ("n[001-002]", None),  # several nodes: not one to blame
        ("n1,n2", None),
    ],
)
def test_node_parsing(raw: str, node: str | None) -> None:
    sq = SqueueJob.model_validate({"job_id": 1, "job_state": ["NODE_FAIL"], "nodes": raw})
    assert sq.info().node == node
    sa = SacctJob.model_validate(
        {"job_id": 1, "state": {"current": ["NODE_FAIL"], "reason": "None"}, "nodes": raw}
    )
    assert sa.info().node == node
    info = SlurmJobInfo(job_id=sq.info().job_id, state="NODE_FAIL", source="squeue", node=node)
    assert job_status(info).node == node
    # A pending job has no node, even one SLURM plans to start it on.
    planned = SlurmJobInfo(job_id=sq.info().job_id, state="PENDING", source="squeue", node="n1")
    assert job_status(planned).node is None
    # A field SLURM leaves out is no node either.
    assert SqueueJob.model_validate({"job_id": 1, "job_state": ["PENDING"]}).info().node is None
