from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ExecutorError
from ebs.exec.slurm.cli import (
    ArraySpec,
    CommandResult,
    SbatchOptions,
    SlurmCli,
    SubprocessRunner,
    sbatch_argv,
    write_env_file,
)
from ebs.exec.slurm.models import ArrayTaskId, JobId, SlurmJobInfo

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "slurm" / "26.05"

Handler = Callable[[list[str]], CommandResult]


class FakeRunner:
    """Answers SLURM commands from per-command handlers and records every argv."""

    def __init__(self, **handlers: Handler | list[CommandResult]) -> None:
        self.handlers = handlers
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        assert isinstance(argv, list | tuple), "argv must be a list, never a shell string"
        argv = list(argv)
        self.calls.append(argv)
        self.timeouts.append(timeout_s)
        handler = self.handlers[Path(argv[0]).name]
        if isinstance(handler, list):
            return handler.pop(0)
        return handler(argv)

    def commands(self) -> list[str]:
        return [Path(c[0]).name for c in self.calls]


def ok(stdout: str = "") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


def err(stderr: str, code: int = 1) -> CommandResult:
    return CommandResult(returncode=code, stdout="", stderr=stderr)


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def _doc(*jobs: dict[str, Any], squeue: bool = True) -> str:
    base = json.loads(fixture_text("squeue_empty.json" if squeue else "sacct_finished.json"))
    base["jobs"] = list(jobs)
    return json.dumps(base)


def _squeue_job(job_id: int, state: str = "RUNNING", **extra: Any) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "job_state": [state],
        "state_reason": "None",
        "array_job_id": {"set": True, "infinite": False, "number": 0},
        "array_task_id": {"set": False, "infinite": False, "number": 0},
        **extra,
    }


def _sacct_job(job_id: int, state: str = "COMPLETED", rc: int = 0) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "state": {"current": [state], "reason": "None"},
        "array": {
            "job_id": 0,
            "task": "",
            "task_id": {"set": False, "infinite": False, "number": 0},
        },
        "exit_code": {
            "status": ["SUCCESS" if rc == 0 else "ERROR"],
            "return_code": {"set": True, "infinite": False, "number": rc},
            "signal": {"id": {"set": False, "infinite": False, "number": 0}, "name": ""},
        },
    }


def make_cli(runner: FakeRunner, clock: FakeClock | None = None, **kw: Any) -> SlurmCli:
    return SlurmCli(runner=runner, clock=clock or FakeClock(), **kw)


# --- R1: sbatch argv -------------------------------------------------------------------------

SCRIPT = Path("/w/job.sh")


# R1
@pytest.mark.parametrize(
    ("opts", "expected"),
    [
        pytest.param(
            SbatchOptions(job_name="ebs-lint"),
            [
                "sbatch",
                "--parsable",
                "--job-name=ebs-lint",
                "--export=NONE",
                "--no-requeue",
                "/w/job.sh",
            ],
            id="minimal",
        ),
        pytest.param(
            SbatchOptions(
                job_name="ebs-sim",
                partition="p1",
                account="a1",
                qos="q1",
                cpus_per_task=2,
                mem_mb=8192,
                time_limit_s=30 * 60 + 5,
                licenses={"vcspyglass": 1, "msimhdlsim@flexlm": 2},
                constraint="os-img1&avx2",
                output=Path("/d/%A_%a.out"),
                error=Path("/d/%A_%a.err"),
                workdir=Path("/w"),
                export_file=Path("/w/env.nul"),
            ),
            [
                "sbatch",
                "--parsable",
                "--job-name=ebs-sim",
                "--partition=p1",
                "--account=a1",
                "--qos=q1",
                "--cpus-per-task=2",
                "--mem=8192M",
                "--time=0-00:30:05",
                "--licenses=msimhdlsim@flexlm:2,vcspyglass:1",
                "--constraint=os-img1&avx2",
                "--output=/d/%A_%a.out",
                "--error=/d/%A_%a.err",
                "--chdir=/w",
                "--export=NONE",
                "--export-file=/w/env.nul",
                "--no-requeue",
                "/w/job.sh",
            ],
            id="full",
        ),
        pytest.param(
            SbatchOptions(
                job_name="ebs-regress",
                array=ArraySpec(indices=(0, 1, 2, 3, 7, 9, 10), throttle=200),
                time_limit_s=2 * 86400 + 3600,
            ),
            [
                "sbatch",
                "--parsable",
                "--job-name=ebs-regress",
                "--time=2-01:00:00",
                "--array=0-3,7,9-10%200",
                "--export=NONE",
                "--no-requeue",
                "/w/job.sh",
            ],
            id="array-throttle",
        ),
        pytest.param(
            SbatchOptions(job_name="x", array=ArraySpec(indices=(5,)), requeue=True),
            [
                "sbatch",
                "--parsable",
                "--job-name=x",
                "--array=5",
                "--export=NONE",
                "--requeue",
                "/w/job.sh",
            ],
            id="array-single-requeue",
        ),
    ],
)
def test_sbatch_argv(opts: SbatchOptions, expected: list[str]) -> None:
    assert sbatch_argv(SCRIPT, opts) == expected


# R1: user strings reach sbatch as single argv elements, untouched by any shell.
def test_no_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="42\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = SlurmCli(runner=SubprocessRunner(), clock=FakeClock())
    nasty = "a; rm -rf / $(id) `x` 'q\" *"
    job = cli.submit(Path("/w/my job;$(id).sh"), SbatchOptions(job_name=nasty))
    assert job == JobId("42")
    assert isinstance(seen["argv"], list)
    assert seen.get("shell", False) is False
    assert f"--job-name={nasty}" in seen["argv"]
    assert seen["argv"][-1] == "/w/my job;$(id).sh"


# R1
@pytest.mark.parametrize(
    "bad",
    [
        {"job_name": "a\nb"},
        {"job_name": ""},
        {"job_name": "x", "partition": "p 1"},
        {"job_name": "x", "account": "a,b"},
        {"job_name": "x", "qos": ""},
        {"job_name": "x", "constraint": "a\x00b"},
        {"job_name": "x", "licenses": {"bad name": 1}},
        {"job_name": "x", "licenses": {"ok": 0}},
        {"job_name": "x", "cpus_per_task": 0},
        {"job_name": "x", "mem_mb": 0},
        {"job_name": "x", "time_limit_s": 0},
    ],
)
def test_invalid_options_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - pydantic ValidationError is a ValueError
        SbatchOptions(**bad)


# R1
@pytest.mark.parametrize(
    "bad",
    [
        {"indices": ()},
        {"indices": (1, 1)},
        {"indices": (-1,)},
        {"indices": (0,), "throttle": 0},
    ],
)
def test_invalid_array_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        ArraySpec(**bad)


# R1: --parsable prints "id" or "id;cluster"; sbatch warnings on stderr are not errors.
@pytest.mark.parametrize(("stdout", "expected"), [("1234\n", "1234"), ("1234;c1\n", "1234")])
def test_submit_parses_parsable_output(stdout: str, expected: str) -> None:
    runner = FakeRunner(
        sbatch=[CommandResult(0, stdout, "sbatch: warning: can't honor --ntasks-per-node")]
    )
    assert make_cli(runner).submit(SCRIPT, SbatchOptions(job_name="j")) == JobId(expected)


# R1
def test_submit_rejects_garbage_output() -> None:
    runner = FakeRunner(sbatch=[ok("Submitted batch job 12\n")])
    with pytest.raises(ExecutorError, match="Submitted batch job 12"):
        make_cli(runner).submit(SCRIPT, SbatchOptions(job_name="j"))


# R1
def test_bin_dir_prefixes_commands(tmp_path: Path) -> None:
    runner = FakeRunner(sbatch=[ok("1\n")])
    make_cli(runner, bin_dir=tmp_path).submit(SCRIPT, SbatchOptions(job_name="j"))
    assert runner.calls[0][0] == str(tmp_path / "sbatch")


# R1: the env file is NUL-separated NAME=value, so values may hold newlines and quotes.
def test_write_env_file(tmp_path: Path) -> None:
    path = tmp_path / "env"
    write_env_file(path, {"B": "two\nlines", "A": "x'y\"z"})
    assert path.read_bytes() == b"A=x'y\"z\x00B=two\nlines\x00"


# R1
@pytest.mark.parametrize("env", [{"1A": "x"}, {"A=B": "x"}, {"A": "nul\x00"}])
def test_write_env_file_rejects_bad_entries(tmp_path: Path, env: dict[str, str]) -> None:
    with pytest.raises(ExecutorError):
        write_env_file(tmp_path / "env", env)


# --- R2: query -------------------------------------------------------------------------------


# R2: one squeue call for everything, one sacct call only for what squeue no longer knows.
def test_query_batches_calls() -> None:
    squeue = _doc(_squeue_job(11), _squeue_job(12, "PENDING", state_reason="Priority"))
    sacct = _doc(_sacct_job(13), _sacct_job(14, "FAILED", 75), squeue=False)
    runner = FakeRunner(squeue=[ok(squeue)], sacct=[ok(sacct)])
    ids = [JobId("12"), JobId("11"), JobId("14"), JobId("13")]
    result = make_cli(runner).query(ids)
    assert runner.commands() == ["squeue", "sacct"]
    assert runner.calls[0] == ["squeue", "--json", "--states=all", "--jobs=11,12,13,14"]
    assert runner.calls[1] == ["sacct", "--json", "--jobs=13,14"]
    assert set(result) == set(ids)
    assert result[JobId("11")].state == "RUNNING"
    assert result[JobId("12")].reason == "Priority"
    assert result[JobId("14")].exit_code == 75
    assert result[JobId("14")].source == "sacct"


# R2
def test_query_skips_sacct_when_squeue_knows_everything() -> None:
    runner = FakeRunner(squeue=[ok(_doc(_squeue_job(11)))])
    result = make_cli(runner).query([JobId("11")])
    assert runner.commands() == ["squeue"]
    assert result[JobId("11")].state == "RUNNING"


# R2
def test_query_of_nothing_runs_nothing() -> None:
    runner = FakeRunner()
    assert make_cli(runner).query([]) == {}
    assert runner.calls == []


# R2: squeue fails with "Invalid job id" once every requested job left slurmctld.
def test_query_invalid_job_id_falls_back_to_sacct() -> None:
    runner = FakeRunner(
        squeue=[err("slurm_load_jobs error: Invalid job id specified")],
        sacct=[ok(_doc(_sacct_job(13), squeue=False))],
    )
    result = make_cli(runner).query([JobId("13")])
    assert result[JobId("13")].state == "COMPLETED"


# R2: jobs neither command knows are left out; the caller decides what that means.
def test_query_unknown_jobs_are_absent() -> None:
    runner = FakeRunner(squeue=[ok(_doc())], sacct=[ok(_doc(squeue=False))])
    assert make_cli(runner).query([JobId("99")]) == {}


# R2: array elements are looked up by the array's job id and matched by task id.
def test_query_array_tasks() -> None:
    runner = FakeRunner(squeue=[ok(fixture_text("squeue_mixed.json"))])
    refs = [ArrayTaskId(JobId("1010"), t) for t in (0, 1, 2, 9)]
    result = make_cli(runner).query(refs)
    assert runner.calls[0] == ["squeue", "--json", "--states=all", "--jobs=1010"]
    assert result[refs[0]].state == "RUNNING"
    assert result[refs[0]].job_id == JobId("1011")
    # tasks 2..9 are still one pending record "2-9%2"
    assert result[refs[2]].state == "PENDING"
    assert result[refs[2]].reason == "JobArrayTaskLimit"
    assert result[refs[2]].array_task_id == 2
    assert result[refs[3]].array_task_id == 9


# R2
def test_query_array_task_outside_pending_range_goes_to_sacct() -> None:
    sacct = fixture_text("sacct_array_pending.json")
    squeue = _doc()
    runner = FakeRunner(squeue=[ok(squeue)], sacct=[ok(sacct)])
    refs = [ArrayTaskId(JobId("3000"), t) for t in (0, 3, 7, 5)]
    result = make_cli(runner).query(refs)
    assert runner.calls[1] == ["sacct", "--json", "--jobs=3000"]
    assert result[refs[0]].state == "RUNNING"
    assert result[refs[1]].state == "PENDING"
    assert result[refs[2]].state == "PENDING"
    assert refs[3] not in result  # 5 is not in "1-4,7%1"


# R2: duplicate references are queried once.
def test_query_deduplicates() -> None:
    runner = FakeRunner(squeue=[ok(_doc(_squeue_job(11)))])
    make_cli(runner).query([JobId("11"), JobId("11")])
    assert runner.calls[0][-1] == "--jobs=11"


# R2
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "squeue_mixed.json",
            {
                "1001": ("RUNNING", None, None),
                "1002": ("PENDING", "Licenses", None),
                "1003": ("PENDING", "ReqNodeNotAvail, UnavailableNodes:n003", None),
                "1004": ("PENDING", "Priority", None),
                "1005": ("PENDING", "Resources", None),
                "1006": ("COMPLETED", None, 0),
                "1010_0": ("RUNNING", None, None),
                "1010_1": ("RUNNING", None, None),
                "1010_5": ("PENDING", "JobArrayTaskLimit", None),
            },
        ),
        (
            "squeue_terminal.json",
            {
                "1020": ("COMPLETED", None, 0),
                "1021": ("FAILED", None, 75),
                "1022": ("OUT_OF_MEMORY", None, 0),
                "1023": ("CANCELLED", None, 0),
            },
        ),
        (
            "sacct_finished.json",
            {
                "2001": ("COMPLETED", None, 0),
                "2002": ("FAILED", None, 75),
                "2003": ("OUT_OF_MEMORY", "OutOfMemory", 0),
                "2004": ("TIMEOUT", None, 0),
                "2005": ("NODE_FAIL", "NodeDown", 0),
                "2006": ("PREEMPTED", None, 0),
                "2007": ("CANCELLED", None, 0),
                "2008": ("FAILED", None, 0),
                "2010_0": ("COMPLETED", None, 0),
                "2010_1": ("COMPLETED", None, 0),
                "2010_2": ("COMPLETED", None, 0),
            },
        ),
        (
            "sacct_array_pending.json",
            {"3000_0": ("RUNNING", None, None), "3000_4": ("PENDING", "JobArrayTaskLimit", None)},
        ),
        ("squeue_empty.json", {}),
    ],
)
def test_parse_fixtures(name: str, expected: dict[str, tuple[str, str | None, int | None]]) -> None:
    text = fixture_text(name)
    is_squeue = name.startswith("squeue")
    runner = FakeRunner(
        squeue=[ok(text if is_squeue else _doc())],
        sacct=[ok(_doc(squeue=False) if is_squeue else text)],
    )
    refs = [ArrayTaskId.parse(k) if "_" in k else JobId(k) for k in expected] or [JobId("999")]
    result = make_cli(runner).query(refs)
    got = {str(ref): (i.state, i.reason, i.exit_code) for ref, i in result.items()}
    assert got == expected


# R2: the signal that killed a job is reported next to its exit code.
def test_parse_signal() -> None:
    runner = FakeRunner(squeue=[ok(_doc())], sacct=[ok(fixture_text("sacct_finished.json"))])
    info = make_cli(runner).query([JobId("2008")])[JobId("2008")]
    assert (info.exit_code, info.signal) == (0, 9)


# R2: SLURM adds fields every release; unknown ones are ignored.
def test_query_ignores_unknown_fields() -> None:
    job = _squeue_job(11, brand_new_field={"nested": [1, 2]}, another="x")
    doc = json.loads(_doc(job))
    doc["new_top_level"] = True
    runner = FakeRunner(squeue=[ok(json.dumps(doc))])
    assert make_cli(runner).query([JobId("11")])[JobId("11")].state == "RUNNING"


# R2: older releases render job_state as a plain string.
def test_query_accepts_scalar_job_state() -> None:
    job = _squeue_job(11)
    job["job_state"] = "RUNNING"
    runner = FakeRunner(squeue=[ok(_doc(job))])
    assert make_cli(runner).query([JobId("11")])[JobId("11")].state == "RUNNING"


# R2
@pytest.mark.parametrize("missing", ["job_id", "job_state"])
def test_query_missing_required_field_names_slurm_version(missing: str) -> None:
    job = _squeue_job(11)
    del job[missing]
    runner = FakeRunner(squeue=[ok(_doc(job))])
    with pytest.raises(ExecutorError) as exc:
        make_cli(runner).query([JobId("11")])
    assert "26.05.0" in str(exc.value)
    assert missing in str(exc.value)
    assert "squeue" in str(exc.value)


# R2
def test_query_sacct_missing_state_names_slurm_version() -> None:
    job = _sacct_job(13)
    del job["state"]
    runner = FakeRunner(squeue=[ok(_doc())], sacct=[ok(_doc(job, squeue=False))])
    with pytest.raises(ExecutorError, match=r"(?s)sacct.*26\.05\.0.*state"):
        make_cli(runner).query([JobId("13")])


# R2
def test_query_non_json_output_is_an_executor_error() -> None:
    runner = FakeRunner(squeue=[ok("NOT JSON")])
    with pytest.raises(ExecutorError, match="squeue"):
        make_cli(runner).query([JobId("11")])


# R2
def test_job_refs_round_trip() -> None:
    ref = ArrayTaskId.parse("1234_5")
    assert ref == ArrayTaskId(JobId("1234"), 5)
    assert str(ref) == "1234_5"
    for bad in ("1234", "a_1", "1_b", "1_-1", "_1"):
        with pytest.raises(ExecutorError):
            ArrayTaskId.parse(bad)


# --- R4: transient vs permanent failures ----------------------------------------------------


# R4
@pytest.mark.parametrize(
    "message",
    [
        "squeue: error: slurm_load_jobs error: Socket timed out on send/recv operation",
        "squeue: error: slurm_load_jobs error: "
        "Unable to contact slurm controller (connect failure)",
        "sbatch: error: Batch job submission failed: Slurm backup controller in standby mode",
        "slurmctld not responding",
        "sacct: error: Problem talking to the database: Connection refused",
        "squeue: error: Resource temporarily unavailable",
    ],
)
def test_transient_retry(message: str) -> None:
    clock = FakeClock()
    runner = FakeRunner(squeue=[err(message), err(message), ok(_doc(_squeue_job(11)))])
    result = make_cli(runner, clock).query([JobId("11")])
    assert JobId("11") in result
    assert runner.commands() == ["squeue"] * 3
    assert clock.monotonic() == pytest.approx(1.0 + 2.0)  # backoff 1 s, 2 s


# R4: at most 5 retries, with growing delays, then an error that quotes SLURM.
def test_transient_retry_gives_up_after_five_retries() -> None:
    clock = FakeClock()
    msg = "sbatch: error: Batch job submission failed: Socket timed out on send/recv operation"
    runner = FakeRunner(sbatch=[err(msg) for _ in range(10)])
    with pytest.raises(ExecutorError) as exc:
        make_cli(runner, clock).submit(SCRIPT, SbatchOptions(job_name="j"))
    assert len(runner.calls) == 6
    assert clock.monotonic() == pytest.approx(1 + 2 + 4 + 8 + 16)
    assert "Socket timed out" in str(exc.value)
    assert "6 attempts" in str(exc.value)


# R4: a command that hangs past its timeout counts as transient.
def test_command_timeout_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def fake_run(argv: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, 0, stdout="7\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli = SlurmCli(runner=SubprocessRunner(), clock=FakeClock())
    assert cli.submit(SCRIPT, SbatchOptions(job_name="j")) == JobId("7")
    assert len(calls) == 2


# R4
@pytest.mark.parametrize(
    "message",
    [
        "sbatch: error: Batch job submission failed: "
        "Invalid account or account/partition combination specified",
        "sbatch: error: Batch job submission failed: Invalid qos specification",
        "sbatch: error: Batch job submission failed: Job violates accounting/QOS policy "
        "(job submit limit, user's size and/or time limits)",
        "sbatch: error: Invalid --licenses specification",
    ],
)
def test_permanent_error_message(message: str) -> None:
    clock = FakeClock()
    runner = FakeRunner(sbatch=[err(message)])
    with pytest.raises(ExecutorError) as exc:
        make_cli(runner, clock).submit(SCRIPT, SbatchOptions(job_name="ebs-x", account="a1"))
    assert len(runner.calls) == 1
    assert clock.monotonic() == 0.0
    assert message in str(exc.value)
    assert "sbatch" in str(exc.value)


# R4: a missing SLURM client is a clear, permanent error.
def test_missing_command_is_actionable(tmp_path: Path) -> None:
    cli = SlurmCli(runner=SubprocessRunner(), clock=FakeClock(), bin_dir=tmp_path)
    with pytest.raises(ExecutorError, match=r"(?s)sbatch.*not found.*bin_dir"):
        cli.submit(SCRIPT, SbatchOptions(job_name="j"))


# --- cancel / version ------------------------------------------------------------------------


# R4
def test_cancel_one_call_for_all_jobs() -> None:
    runner = FakeRunner(scancel=[ok()])
    make_cli(runner).cancel([JobId("12"), ArrayTaskId(JobId("10"), 3), JobId("12")])
    assert runner.calls == [["scancel", "10_3", "12"]]


# R4
def test_cancel_of_nothing_runs_nothing() -> None:
    runner = FakeRunner()
    make_cli(runner).cancel([])
    assert runner.calls == []


# R4: cancelling a job that already ended is not an error (Executor.cancel contract).
@pytest.mark.parametrize(
    "message",
    [
        "scancel: error: Kill job error on job id 12: Job/step already completing or completed",
        "scancel: error: Kill job error on job id 12: Invalid job id specified",
    ],
)
def test_cancel_finished_job_is_quiet(message: str) -> None:
    runner = FakeRunner(scancel=[err(message)])
    make_cli(runner).cancel([JobId("12")])


# R4
def test_cancel_other_errors_raise() -> None:
    message = (
        "scancel: error: Kill job error on job id 12: Invalid job id specified\n"
        "scancel: error: Kill job error on job id 13: Access/permission denied"
    )
    runner = FakeRunner(scancel=[err(message)])
    with pytest.raises(ExecutorError, match="permission denied"):
        make_cli(runner).cancel([JobId("12"), JobId("13")])


# R1
def test_version() -> None:
    runner = FakeRunner(scontrol=[ok("slurm 26.05.0\n")])
    assert make_cli(runner).version() == "26.05.0"
    assert runner.calls == [["scontrol", "--version"]]


# R1
def test_info_is_hashable_value() -> None:
    a = SlurmJobInfo(job_id=JobId("1"), state="RUNNING", source="squeue")
    b = SlurmJobInfo(job_id=JobId("1"), state="RUNNING", source="squeue")
    assert a == b
    assert hash(a) == hash(b)


# R4: a harmless line next to a transient one still retries the transient part.
def test_cancel_mixed_harmless_and_transient_retries() -> None:
    clock = FakeClock()
    runner = FakeRunner(
        scancel=[
            err(
                "scancel: error: Kill job error on job id 12: Invalid job id specified\n"
                "scancel: error: Kill job error on job id 13: "
                "Socket timed out on send/recv operation"
            ),
            ok(),
        ]
    )
    make_cli(runner, clock).cancel([JobId("12"), JobId("13")])
    assert len(runner.calls) == 2
    assert clock.monotonic() == 1.0
