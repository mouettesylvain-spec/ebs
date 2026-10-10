"""The SLURM adapter against a real cluster (P1-01 R6). Nightly only (`-m grid`).

Needs the SLURM client on PATH and `EBS_GRID_PARTITION`; optional `EBS_GRID_ACCOUNT`,
`EBS_GRID_QOS`. Jobs are tiny (`sleep 1`) and write their logs under pytest's tmp_path, which
must be on a filesystem the compute nodes share (set `--basetemp` accordingly).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from ebs.exec.slurm.cli import ArraySpec, SbatchOptions, SlurmCli, write_env_file
from ebs.exec.slurm.models import ArrayTaskId, JobRef, SlurmJobInfo
from ebs.exec.slurm.states import job_status
from tests.helpers.wait import wait_until

TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
}


@pytest.fixture
def cli() -> SlurmCli:
    if shutil.which("sbatch") is None:
        pytest.skip("missing dependency: SLURM client (sbatch) on PATH")
    if not os.environ.get("EBS_GRID_PARTITION"):
        pytest.skip("missing dependency: EBS_GRID_PARTITION naming a partition for test jobs")
    return SlurmCli()


def _opts(name: str, tmp_path: Path, **kw: object) -> SbatchOptions:
    return SbatchOptions(
        job_name=f"ebs-grid-{name}",
        partition=os.environ["EBS_GRID_PARTITION"],
        account=os.environ.get("EBS_GRID_ACCOUNT") or None,
        qos=os.environ.get("EBS_GRID_QOS") or None,
        time_limit_s=120,
        mem_mb=256,
        output=tmp_path / "%x-%A_%a.out",
        workdir=tmp_path,
        **kw,  # type: ignore[arg-type]
    )


def _wait(cli: SlurmCli, refs: list[JobRef], states: set[str]) -> dict[JobRef, SlurmJobInfo]:
    last: dict[JobRef, SlurmJobInfo] = {}

    def check() -> bool:
        last.clear()
        last.update(cli.query(refs))
        return len(last) == len(refs) and all(
            i.state in states and "COMPLETING" not in i.flags for i in last.values()
        )

    wait_until(check, timeout=600, interval=5, what=f"{refs} in {sorted(states)}: {last}")
    return last


# R6
def test_submit_array_and_cancel(cli: SlurmCli, tmp_path: Path) -> None:
    assert cli.version().split(".")[0].isdigit()
    script = tmp_path / "job.sh"
    script.write_text('#!/bin/sh\necho "task=${SLURM_ARRAY_TASK_ID:-none} foo=$FOO"\nsleep 1\n')
    script.chmod(0o755)
    env = tmp_path / "env"
    write_env_file(env, {"FOO": "bar", "PATH": "/usr/bin:/bin"})

    single = cli.submit(script, _opts("single", tmp_path, export_file=env))
    arr = cli.submit(
        script,
        _opts("array", tmp_path, export_file=env, array=ArraySpec(indices=(0, 1, 2), throttle=2)),
    )
    refs: list[JobRef] = [single, *(ArrayTaskId(arr, t) for t in range(3))]
    done = _wait(cli, refs, TERMINAL)
    assert {i.state for i in done.values()} == {"COMPLETED"}
    assert all(job_status(i).state == "done" for i in done.values())
    out = (tmp_path / f"ebs-grid-array-{arr}_1.out").read_text()
    assert "task=1 foo=bar" in out

    long_script = tmp_path / "long.sh"
    long_script.write_text("#!/bin/sh\nsleep 300\n")
    long_script.chmod(0o755)
    victim = cli.submit(long_script, _opts("cancel", tmp_path))
    cli.cancel([victim])
    info = _wait(cli, [victim], TERMINAL)[victim]
    assert info.state == "CANCELLED"
    cli.cancel([victim])  # already ended: no error
