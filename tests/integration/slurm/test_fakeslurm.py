"""FakeSlurm behaviour, driven through the real adapter (P1-01 R5)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ExecutorError
from ebs.exec.api import JobStatus
from ebs.exec.slurm.cli import ArraySpec, SbatchOptions, SlurmCli, write_env_file
from ebs.exec.slurm.models import (
    ArrayTaskId,
    JobRef,
    SacctResponse,
    SlurmJobInfo,
    SqueueResponse,
)
from ebs.exec.slurm.states import job_status
from tests.fakes.slurm import FakeSlurm
from tests.fakes.slurm.core import NODE
from tests.helpers.wait import wait_until

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "slurm" / "26.05"
TERMINAL = {
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
}


def _status(cli: SlurmCli, ref: JobRef) -> SlurmJobInfo:
    return cli.query([ref])[ref]


def _wait_state(
    cli: SlurmCli, ref: JobRef, states: set[str], timeout: float = 20.0
) -> SlurmJobInfo:
    seen: dict[str, SlurmJobInfo] = {}

    def check() -> bool:
        got = cli.query([ref]).get(ref)
        if got is not None:
            seen["info"] = got
        return got is not None and got.state in states and "COMPLETING" not in got.flags

    wait_until(check, timeout=timeout, what=f"{ref} in {sorted(states)} (last: {seen})")
    return seen["info"]


def _sh(fake: FakeSlurm, name: str, body: str) -> Path:
    return fake.script(name, "#!/bin/sh\n" + body)


# R5
def test_submit_runs_script_and_reports_exit_code(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    cli = fake_slurm.cli()
    ok = cli.submit(
        _sh(fake_slurm, "ok.sh", "echo hello\n"),
        SbatchOptions(job_name="ok", output=tmp_path / "%x-%j.out"),
    )
    bad = cli.submit(_sh(fake_slurm, "bad.sh", "exit 75\n"), SbatchOptions(job_name="bad"))
    info = _wait_state(cli, ok, TERMINAL)
    assert (info.state, info.exit_code) == ("COMPLETED", 0)
    assert (tmp_path / f"ok-{ok}.out").read_text() == "hello\n"
    info = _wait_state(cli, bad, TERMINAL)
    assert (info.state, info.exit_code) == ("FAILED", 75)
    assert job_status(info) == JobStatus(
        state="infra_failed", infra_reason="other", exit_code=75, node=NODE
    )


# R5: at most N elements of an array run at once.
def test_array_throttle(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    fake_slurm.configure(cpus=8)
    work = tmp_path / "w"
    work.mkdir()
    script = _sh(
        fake_slurm,
        "task.sh",
        f"cd {work}\n"
        'touch "running.$SLURM_ARRAY_TASK_ID"\n'
        'ls running.* | wc -l > "count.$SLURM_ARRAY_TASK_ID"\n'
        f"while [ ! -e {work}/gate ]; do sleep 0.05; done\n"
        "sleep 0.2\n"
        'rm "running.$SLURM_ARRAY_TASK_ID"\n',
    )
    cli = fake_slurm.cli()
    job = cli.submit(
        script, SbatchOptions(job_name="arr", array=ArraySpec(indices=tuple(range(6)), throttle=2))
    )
    refs = [ArrayTaskId(job, t) for t in range(6)]
    first = cli.query(refs)
    assert len(first) == 6
    pending = [i for i in first.values() if i.state == "PENDING"]
    assert pending
    assert {i.reason for i in pending} == {"JobArrayTaskLimit"}
    assert len(pending) == 4  # two run, held by the gate; the rest wait for the throttle
    (work / "gate").touch()
    for ref in refs:
        _wait_state(cli, ref, TERMINAL)
    counts = [int((work / f"count.{t}").read_text()) for t in range(6)]
    assert max(counts) <= 2
    assert all(i.state == "COMPLETED" for i in cli.query(refs).values())
    # each element saw its own id and the array's
    assert {i.array_task_id for i in cli.query(refs).values()} == set(range(6))


# R5: a job whose license pool is exhausted stays PENDING (Licenses) until one is free.
def test_license_pool_blocks(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    fake_slurm.configure(licenses={"msimhdlsim": 1})
    gate = tmp_path / "gate"
    script = _sh(fake_slurm, "hold.sh", f"while [ ! -e {gate} ]; do sleep 0.05; done\n")
    cli = fake_slurm.cli()
    opts = SbatchOptions(job_name="lic", licenses={"msimhdlsim": 1})
    first = cli.submit(script, opts)
    second = cli.submit(script, opts)
    _wait_state(cli, first, {"RUNNING"})
    info = _status(cli, second)
    assert (info.state, info.reason) == ("PENDING", "Licenses")
    assert job_status(info) == JobStatus(state="pending", pending_reason="licenses")
    gate.touch()
    assert _wait_state(cli, first, TERMINAL).state == "COMPLETED"
    assert _wait_state(cli, second, TERMINAL).state == "COMPLETED"


# R5: CPU capacity is a pool too; jobs that don't fit wait with reason Resources.
def test_cpu_pool_blocks(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    fake_slurm.configure(cpus=2)
    gate = tmp_path / "gate"
    script = _sh(fake_slurm, "hold.sh", f"while [ ! -e {gate} ]; do sleep 0.05; done\n")
    cli = fake_slurm.cli()
    first = cli.submit(script, SbatchOptions(job_name="a", cpus_per_task=2))
    second = cli.submit(script, SbatchOptions(job_name="b", cpus_per_task=1))
    _wait_state(cli, first, {"RUNNING"})
    info = _status(cli, second)
    assert (info.state, info.reason) == ("PENDING", "Resources")
    gate.touch()
    assert _wait_state(cli, second, TERMINAL).state == "COMPLETED"


# R5: exceeding --mem kills the job with OUT_OF_MEMORY.
def test_oom_kill(fake_slurm: FakeSlurm) -> None:
    script = fake_slurm.script(
        "hog.py",
        f"#!{sys.executable}\nimport time\nb = b'x' * (200 * 1024 * 1024)\ntime.sleep(30)\n",
    )
    cli = fake_slurm.cli()
    job = cli.submit(script, SbatchOptions(job_name="hog", mem_mb=50))
    info = _wait_state(cli, job, TERMINAL)
    assert info.state == "OUT_OF_MEMORY"
    assert job_status(info).infra_reason == "oom"


# R5: exceeding --time kills the job with TIMEOUT (time_scale shrinks SLURM seconds).
def test_time_kill(fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(time_scale=0.01)  # 60 s limit -> 0.6 s
    cli = fake_slurm.cli()
    job = cli.submit(
        _sh(fake_slurm, "slow.sh", "sleep 30\n"), SbatchOptions(job_name="slow", time_limit_s=60)
    )
    info = _wait_state(cli, job, TERMINAL)
    assert info.state == "TIMEOUT"
    assert job_status(info).infra_reason == "timeout"


# R5
@pytest.mark.parametrize(
    ("fault", "state", "expected"),
    [
        pytest.param(
            {"fault": "NODE_FAIL", "after_s": 0.2},
            "NODE_FAIL",
            JobStatus(state="infra_failed", infra_reason="node_fail", exit_code=-9, node=NODE),
            id="node-fail",
        ),
        pytest.param(
            {"fault": "PREEMPTED", "after_s": 0.2},
            "PREEMPTED",
            JobStatus(state="infra_failed", infra_reason="preempted", exit_code=-15, node=NODE),
            id="preempted",
        ),
    ],
)
def test_fault_injection(
    fake_slurm: FakeSlurm, fault: dict[str, Any], state: str, expected: JobStatus
) -> None:
    fake_slurm.inject({**fault, "job_name": "victim*"})
    cli = fake_slurm.cli()
    script = _sh(fake_slurm, "long.sh", "sleep 30\n")
    victim = cli.submit(script, SbatchOptions(job_name="victim-1"))
    bystander = cli.submit(_sh(fake_slurm, "quick.sh", "true\n"), SbatchOptions(job_name="other"))
    info = _wait_state(cli, victim, TERMINAL)
    assert info.state == state
    assert job_status(info) == expected
    assert _wait_state(cli, bystander, TERMINAL).state == "COMPLETED"


# R5: a job can be held in PENDING (Licenses) indefinitely.
def test_fault_injection_stuck_pending_licenses(fake_slurm: FakeSlurm) -> None:
    fake_slurm.inject({"fault": "STUCK_LICENSES", "job_name": "stuck"})
    cli = fake_slurm.cli()
    job = cli.submit(_sh(fake_slurm, "x.sh", "true\n"), SbatchOptions(job_name="stuck"))
    for _ in range(3):
        info = _status(cli, job)
        assert (info.state, info.reason) == ("PENDING", "Licenses")
    fake_slurm.inject()  # clear: the job starts on the next scheduling pass
    assert _wait_state(cli, job, TERMINAL).state == "COMPLETED"


# R5 + R4: the fake can make a command fail transiently; the adapter retries through it.
def test_fault_injection_transient_cli(fake_slurm: FakeSlurm) -> None:
    fake_slurm.inject(
        {
            "fault": "CLI_ERROR",
            "command": "sbatch",
            "count": 2,
            "message": "sbatch: error: Batch job submission failed: "
            "Socket timed out on send/recv operation",
        }
    )
    clock = FakeClock()
    cli = fake_slurm.cli(clock=clock)
    job = cli.submit(_sh(fake_slurm, "x.sh", "true\n"), SbatchOptions(job_name="j"))
    assert clock.monotonic() == 1.0 + 2.0  # backoff 1 s, 2 s
    assert _wait_state(cli, job, TERMINAL).state == "COMPLETED"


# P1-01 follow-up: the controller accepted the job but the reply timed out. The retry finds
# the job by the token in its comment instead of submitting a second one.
def test_lost_sbatch_reply_submits_once(fake_slurm: FakeSlurm) -> None:
    fake_slurm.inject(
        {
            "fault": "CLI_ERROR",
            "command": "sbatch",
            "count": 1,
            "accept": True,
            "message": "sbatch: error: Batch job submission failed: "
            "Socket timed out on send/recv operation",
        }
    )
    cli = fake_slurm.cli(clock=FakeClock())
    uuid = "0b9f3c3e-6a51-4c0e-9d7e-2f4d6f1a2b3c"
    job = cli.submit(_sh(fake_slurm, "x.sh", "true\n"), SbatchOptions(job_name="j", comment=uuid))
    assert _wait_state(cli, job, TERMINAL).state == "COMPLETED"
    out = subprocess.run(
        [str(fake_slurm.bin_dir / "squeue"), "--json", "--states=all", "--name=j"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    jobs = json.loads(out)["jobs"]
    assert [j["job_id"] for j in jobs] == [int(job)]
    assert jobs[0]["comment"].startswith(f"{uuid} ebs-submit=")


# R5 + R4: permanent submission errors come back with SLURM's message.
def test_permanent_errors(fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(accounts=["a1"], qos=["q1"], licenses={"msimhdlsim": 2})
    cli = fake_slurm.cli()
    script = _sh(fake_slurm, "x.sh", "true\n")
    with pytest.raises(ExecutorError, match="Invalid account"):
        cli.submit(script, SbatchOptions(job_name="j", account="a2"))
    with pytest.raises(ExecutorError, match="Invalid qos"):
        cli.submit(script, SbatchOptions(job_name="j", account="a1", qos="q9"))
    with pytest.raises(ExecutorError, match="Invalid license"):
        cli.submit(script, SbatchOptions(job_name="j", licenses={"nosuch": 1}))
    no_shebang = fake_slurm.script("plain.sh", "true\n")
    with pytest.raises(ExecutorError, match="#!"):
        cli.submit(no_shebang, SbatchOptions(job_name="j"))


# R5: scancel stops running jobs and drops pending ones.
def test_cancel(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    fake_slurm.configure(cpus=1)
    cli = fake_slurm.cli()
    script = _sh(fake_slurm, "long.sh", "sleep 30\n")
    running = cli.submit(script, SbatchOptions(job_name="r"))
    pending = cli.submit(script, SbatchOptions(job_name="p"))
    _wait_state(cli, running, {"RUNNING"})
    assert _status(cli, pending).state == "PENDING"
    cli.cancel([running, pending])
    assert _wait_state(cli, pending, TERMINAL).state == "CANCELLED"
    info = _wait_state(cli, running, TERMINAL)
    assert info.state == "CANCELLED"
    assert job_status(info).state == "cancelled"
    cli.cancel([running])  # already ended: not an error


# R5 + R1: --export=NONE hides the submitter's environment; the env file is all the job sees.
def test_export_none_and_env_file(
    fake_slurm: FakeSlurm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUBMITTER_SECRET", "leak")
    env_file = tmp_path / "env"
    write_env_file(env_file, {"FOO": "bar baz", "MULTI": "a\nb"})
    out = tmp_path / "env.out"
    script = fake_slurm.script(
        "env.py",
        f"#!{sys.executable}\nimport json, os\n"
        f"open({str(out)!r}, 'w').write(json.dumps(dict(os.environ)))\n",
    )
    cli = fake_slurm.cli()
    job = cli.submit(script, SbatchOptions(job_name="env", export_file=env_file))
    _wait_state(cli, job, TERMINAL)
    env = json.loads(out.read_text())
    assert env["FOO"] == "bar baz"
    assert env["MULTI"] == "a\nb"
    assert "SUBMITTER_SECRET" not in env
    assert env["SLURM_JOB_ID"] == job


# R5 + R2: once slurmctld forgets a job, the adapter finds it in sacct.
def test_sacct_after_job_ages_out(fake_slurm: FakeSlurm) -> None:
    fake_slurm.configure(min_job_age_s=0)
    cli = fake_slurm.cli()
    job = cli.submit(_sh(fake_slurm, "x.sh", "exit 3\n"), SbatchOptions(job_name="j"))
    info = _wait_state(cli, job, TERMINAL)
    assert (info.source, info.state, info.exit_code) == ("sacct", "FAILED", 3)


def _raw(fake: FakeSlurm, *argv: str) -> dict[str, Any]:
    done = subprocess.run(
        [str(fake.bin_dir / argv[0]), *argv[1:]], capture_output=True, text=True, check=True
    )
    data: dict[str, Any] = json.loads(done.stdout)
    return data


def _fixture_keys(name: str) -> set[str]:
    jobs = json.loads((FIXTURES / name).read_text())["jobs"]
    return set().union(*(set(j) for j in jobs))


# R5: the fake's JSON parses with the models used on the 26.05 fixtures, and invents no fields.
def test_json_matches_models_and_fixtures(fake_slurm: FakeSlurm, tmp_path: Path) -> None:
    fake_slurm.configure(cpus=1)
    cli = fake_slurm.cli()
    gate = tmp_path / "gate"
    hold = _sh(fake_slurm, "hold.sh", f"while [ ! -e {gate} ]; do sleep 0.05; done\n")
    first = cli.submit(hold, SbatchOptions(job_name="a"))
    arr = cli.submit(hold, SbatchOptions(job_name="b", array=ArraySpec(indices=(0, 1, 2))))
    _wait_state(cli, first, {"RUNNING"})
    jobs = f"--jobs={first},{arr}"
    squeue = _raw(fake_slurm, "squeue", "--json", "--states=all", jobs)
    sacct = _raw(fake_slurm, "sacct", "--json", jobs)
    SqueueResponse.model_validate(squeue)
    SacctResponse.model_validate(sacct)
    assert {j["array_task_string"] for j in squeue["jobs"]} >= {"0-2"}
    gate.touch()
    for t in range(3):
        _wait_state(cli, ArrayTaskId(arr, t), TERMINAL)
    squeue = _raw(fake_slurm, "squeue", "--json", "--states=all", jobs)
    sacct = _raw(fake_slurm, "sacct", "--json", jobs)
    SqueueResponse.model_validate(squeue)
    SacctResponse.model_validate(sacct)
    for doc, fixture in ((squeue, "squeue_mixed.json"), (sacct, "sacct_finished.json")):
        assert doc["meta"]["slurm"]["release"] == "26.05.0"
        for job in doc["jobs"]:
            assert set(job) <= _fixture_keys(fixture)
    assert cli.version() == "26.05.0"


# R5: a supervisor that dies leaves the job NODE_FAIL, never RUNNING forever.
def test_lost_supervisor_is_node_fail(fake_slurm: FakeSlurm) -> None:
    cli = fake_slurm.cli()
    job = cli.submit(_sh(fake_slurm, "long.sh", "sleep 30\n"), SbatchOptions(job_name="j"))
    _wait_state(cli, job, {"RUNNING"})
    pid = fake_slurm.supervisor_pid(job)
    os.kill(pid, 9)
    assert _wait_state(cli, job, TERMINAL).state == "NODE_FAIL"
