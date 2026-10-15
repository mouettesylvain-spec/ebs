"""Invariant I16 on a (fake) grid: node scratch never outlives a SLURM job (P1-04 R4).

The runner runs as a FakeSlurm batch job. Success, failure and the tool's own timeout are
cleaned by the runner; a SLURM cancel (SIGTERM, then SIGKILL after KillWait) by its signal
handler within the grace; a SIGKILL that leaves no chance at all by the node epilog
(deploy/slurm/epilog.d/ebs-scratch-cleanup.sh), which FakeSlurm runs when the job ends.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import Digest
from tests.fakes.slurm import FakeSlurm
from tests.fakes.slurm.core import REPO_ROOT, TERMINAL, locked
from tests.helpers.runner import shell_spec, source_file, store_plan
from tests.helpers.wait import wait_until

EPILOG = REPO_ROOT / "deploy" / "slurm" / "epilog.d" / "ebs-scratch-cleanup.sh"
KILL_WAIT_S = 10.0  # SLURM's KillWait: the runner must clean up within it after SIGTERM


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSlurm]:
    monkeypatch.chdir(tmp_path)
    fake = FakeSlurm.install(tmp_path / "fakeslurm", kill_wait_s=KILL_WAIT_S)
    yield fake
    fake.shutdown()


def _setup(tmp_path: Path) -> tuple[FsCAS, dict[str, str]]:
    cas = FsCAS(tmp_path / "cas", "test")
    config = tmp_path / "config.toml"
    config.write_text(
        f'[cas]\nroot = "{tmp_path / "cas"}"\n'
        f'[scratch]\ndir = "{tmp_path / "scratch"}"\n'
        "[runner]\nkill_grace_s = 1\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path / "home"),
        "EBS_CONFIG": str(config),
        "PYTHONPATH": str(REPO_ROOT / "src"),
    }
    return cas, env


def _submit(fake: FakeSlurm, tmp_path: Path, plan: Digest, env: dict[str, str]) -> int:
    script = fake.script(
        "runner.sh",
        "#!/bin/sh\n"
        f"exec {sys.executable} -m ebs.runner.main --plan {plan} --action s --domain test\n",
    )
    out = subprocess.run(
        [str(fake.bin_dir / "sbatch"), "--parsable", "-J", "ebs-s",
         f"--comment={uuid.uuid4()} s", "-o", str(tmp_path / "job-%j.out"), str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )  # fmt: skip
    return int(out.stdout.strip())


def _state(fake: FakeSlurm, job: int) -> str:
    with locked(fake.state_dir) as st:
        row = st.job(job)
        assert row is not None
        return str(row["state"])


def _wait_done(fake: FakeSlurm, job: int) -> str:
    wait_until(lambda: _state(fake, job) in TERMINAL, timeout=60, what=f"job {job} to end")
    return _state(fake, job)


def _scratch_left(tmp_path: Path) -> list[Path]:
    root = tmp_path / "scratch"
    return list(root.rglob("*")) if root.exists() else []


def _job_log(tmp_path: Path, job: int) -> str:
    path = tmp_path / f"job-{job}.out"
    return path.read_text() if path.exists() else ""


def _scancel(fake: FakeSlurm, job: int, *args: str) -> None:
    subprocess.run([str(fake.bin_dir / "scancel"), *args, str(job)], check=True)


def _kill_tool(pids: Path) -> None:
    """Real SLURM kills the job's whole cgroup; FakeSlurm only the batch session."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(int(pids.read_text()), signal.SIGKILL)  # the tool leads its own session


def _with_epilog(fake: FakeSlurm, tmp_path: Path) -> None:
    fake.configure(
        epilog=str(EPILOG),
        epilog_env={
            "EBS_SCRATCH_GLOBS": str(tmp_path / "scratch"),
            "EBS_EPILOG_CONF": str(tmp_path / "no-epilog.conf"),
        },
    )


# R4 (I16)
@pytest.mark.parametrize("case", ["success", "fail", "timeout", "sigterm", "sigkill+epilog"])
def test_cleanup(fake: FakeSlurm, tmp_path: Path, case: str) -> None:
    cas, env = _setup(tmp_path)
    ready = tmp_path / "ready"
    pids = tmp_path / "pids"
    src = source_file(cas, "in.txt", b"input")
    script, time = {
        "success": ("echo ok\n", None),
        "fail": ("exit 4\n", None),
        "timeout": ("sleep 60\n", 1),
        "sigterm": (f"touch {ready}; sleep 60\n", None),
        "sigkill+epilog": (f"echo $$ > {pids}; touch {ready}; sleep 60\n", None),
    }[case]
    if case == "sigkill+epilog":
        _with_epilog(fake, tmp_path)
    job = _submit(fake, tmp_path, store_plan(cas, shell_spec(script, inputs=[src], time=time)), env)

    if case in ("sigterm", "sigkill+epilog"):
        wait_until(ready.exists, timeout=60, what="the tool to start")
        assert _scratch_left(tmp_path)  # the job really had scratch to lose
    if case == "sigterm":
        _scancel(fake, job)  # SIGTERM, then SIGKILL after KillWait
    if case == "sigkill+epilog":
        _scancel(fake, job, "--signal=KILL")  # no handler runs: only the epilog can clean

    state = _wait_done(fake, job)
    if case == "sigkill+epilog":
        _kill_tool(pids)
    expected = {"success": "COMPLETED", "sigterm": "CANCELLED", "sigkill+epilog": "FAILED"}
    if case in expected:
        assert state == expected[case], _job_log(tmp_path, job)
    else:  # the runner exits 0 after posting FAILED, 75 after its own timeout
        assert state in ("COMPLETED", "FAILED"), _job_log(tmp_path, job)
    assert _scratch_left(tmp_path) == [], _job_log(tmp_path, job)


# R4: without the epilog a SIGKILL does leave scratch behind, so the epilog case above is real.
def test_sigkill_without_epilog_leaves_scratch(fake: FakeSlurm, tmp_path: Path) -> None:
    cas, env = _setup(tmp_path)
    ready, pids = tmp_path / "ready", tmp_path / "pids"
    spec = shell_spec(f"echo $$ > {pids}; touch {ready}; sleep 60\n")
    job = _submit(fake, tmp_path, store_plan(cas, spec), env)
    wait_until(ready.exists, timeout=60, what="the tool to start")
    _scancel(fake, job, "--signal=KILL")
    _wait_done(fake, job)
    _kill_tool(pids)
    left = _scratch_left(tmp_path)
    assert any(p.name == ".ebs-job" for p in left)
    marker = next(p for p in left if p.name == ".ebs-job")
    assert marker.read_text().strip() == str(job)  # the epilog's key to this job's scratch
