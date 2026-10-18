from __future__ import annotations

import json
import os
import re
import signal
import socket
from pathlib import Path
from typing import Any

import pytest

from ebs.cli._context import EXECUTORS, SiteServices
from ebs.cli._records import BuildRecord
from ebs.cli.build import CacheChoice, ExecutorChoice
from ebs.core.errors import ExitCode
from ebs.driver.cache_policy import CACHE_MODES
from ebs.driver.events import read_events
from ebs.exec.slurm.executor import SlurmExecutor
from ebs.meta.api import BuildId
from ebs.meta.client import TOKEN_ENV
from tests.helpers.cli import Site, TestServices, write_site


# R2
def test_choices_match_the_driver() -> None:
    assert tuple(c.value for c in CacheChoice) == CACHE_MODES
    assert tuple(e.value for e in ExecutorChoice) == EXECUTORS


_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def _interrupt_on_second_poll(n: int) -> None:
    if n == 2:
        os.kill(os.getpid(), signal.SIGINT)  # the driver's handler cancels the build


# R2
@pytest.mark.parametrize(
    ("scripts", "expected"),
    [
        pytest.param({}, ExitCode.OK, id="passed"),
        pytest.param({"gen": ("fail",)}, ExitCode.ACTIONS_FAILED, id="failed"),
        pytest.param({"gen": (("infra", "oom"),)}, ExitCode.INFRA, id="infra_failed"),
        pytest.param({"gen": ("hang",)}, ExitCode.CANCELLED, id="cancelled"),
    ],
)
def test_exit_codes(site: Site, scripts: dict[str, tuple[Any, ...]], expected: ExitCode) -> None:
    site.services.scripts = scripts
    if expected == ExitCode.CANCELLED:
        site.services.on_poll = _interrupt_on_second_poll
    result = site.invoke(["build"])
    assert result.exit_code == expected, result.output
    status = {0: "passed", 1: "failed", 3: "infra_failed", 130: "cancelled"}[expected]
    assert f"build {status}" in result.stdout


# R2
def test_non_tty_events(site: Site) -> None:
    result = site.invoke(["build"])
    assert result.exit_code == ExitCode.OK, result.output
    lines = result.stdout.splitlines()
    first = re.fullmatch(rf"build ({_UUID}) \(id (\d+)\)", lines[0])
    assert first, lines[0]  # the UUID comes before anything else
    uuid = first.group(1)
    assert "submitted gen" in result.stdout
    assert any(re.match(r"finished gen\b.*state=done", line) for line in lines), lines
    assert any(re.match(r"finished sim\[test=smoke\].*state=done", line) for line in lines)
    assert lines[-1] == f"build {uuid}: build passed (done=3)"
    # the local record that `status` and `logs` read
    build_dir = site.proj / ".ebs" / "builds" / uuid
    record = json.loads((build_dir / "build.json").read_text())
    assert record["uuid"] == uuid
    assert record["status"] == "passed"
    assert record["actions"] == {"gen": "gen", "sim[test=smoke]": "sim", "sim[test=random]": "sim"}
    assert read_events(build_dir / "events.jsonl")[-1].type == "build_finished"


# R2
def test_json_lines(site: Site) -> None:
    result = site.invoke(["build", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    docs = [json.loads(line) for line in result.stdout.splitlines()]
    assert docs[0]["type"] == "build_started"
    assert docs[-1]["status"] == "passed"
    assert docs[-1]["exit_code"] == 0
    assert docs[-1]["counts"] == {"done": 3}
    assert re.fullmatch(_UUID, docs[-1]["uuid"])


# R2
def test_cache_flags_reach_the_driver(site: Site) -> None:
    assert site.invoke(["build", "--cache", "write"]).exit_code == ExitCode.OK
    again = site.invoke(["build", "--json"])  # default mode `read` hits what `write` stored
    assert json.loads(again.stdout.splitlines()[-1])["counts"] == {"cached": 3}
    off = site.invoke(["build", "--no-cache", "--json"])
    assert json.loads(off.stdout.splitlines()[-1])["counts"] == {"done": 3}


# R2
def test_rerun_failed_and_keep_going(site: Site) -> None:
    site.services.scripts = {"sim[test=smoke]": ("fail",)}
    first = site.invoke(["build", "--cache", "write", "-k", "--json"])
    assert first.exit_code == ExitCode.ACTIONS_FAILED
    assert json.loads(first.stdout.splitlines()[-1])["counts"] == {"done": 2, "failed": 1}
    site.services.scripts = {}
    again = site.invoke(["build", "--rerun-failed", "--json"])
    assert again.exit_code == ExitCode.OK, again.output
    assert json.loads(again.stdout.splitlines()[-1])["counts"] == {"cached": 2, "done": 1}


# R2
@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["--cache", "sometimes"], id="bad-mode"),
        pytest.param(["--cache", "write", "--no-cache"], id="conflict"),
        pytest.param(["--executor", "lsf"], id="unknown-executor"),
        # P2-08: a resumed build keeps its own plan and cache mode
        pytest.param(["--resume", "1", "--detach"], id="resume-detach"),
        pytest.param(["--resume", "1", "gen"], id="resume-steps"),
        pytest.param(["--resume", "1", "--cache", "write"], id="resume-cache"),
        pytest.param(["--resume", "1", "--resume-into", "1"], id="resume-twice"),
        pytest.param(["--detach"], id="detach-local"),  # local actions in a 1-CPU driver job
        # --ci writes its reports in the CI job: not from a detached or resumed driver
        pytest.param(["--ci", "--detach", "--executor", "slurm"], id="ci-detach"),
        pytest.param(["--ci", "--resume", "1"], id="ci-resume"),
    ],
)
def test_usage_errors(site: Site, args: list[str]) -> None:
    result = site.invoke(["build", *args])
    assert result.exit_code == ExitCode.USAGE, result.output


# P2-08 x P2-07: --ci is refused with a detached or resumed driver, before anything starts.
@pytest.mark.parametrize(
    "args", [["--detach", "--executor", "slurm"], ["--resume", "1"], ["--resume-into", "1"]]
)
def test_ci_needs_its_own_driver(site: Site, args: list[str]) -> None:
    result = site.invoke(["build", "--ci", *args])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "does not combine with --detach or --resume" in result.output
    assert site.services.executors == []


# R2
def test_build_needs_metadata_store(site: Site) -> None:
    site.services.with_store = False
    result = site.invoke(["build"])
    assert result.exit_code == ExitCode.USAGE
    assert "[metadata].url" in result.stderr


# P1-05 R1: `[retry]` reaches the driver; a bad section fails before anything runs.
def test_retry_config_reaches_the_driver(tmp_path: Path) -> None:
    write_site(tmp_path, config_extra="[retry]\nmax_retries = 1\nbackoff_s = 0\n")
    site = Site(tmp_path, TestServices(tmp_path))
    site.services.scripts = {"gen": (("infra", "node_fail"),)}
    assert site.invoke(["build"]).exit_code == ExitCode.INFRA
    assert site.services.executors[-1].runs("gen") == 2  # max_retries = 1
    # A timeout is not retried unless the step asks.
    site.services.scripts = {"gen": (("infra", "timeout"),)}
    assert site.invoke(["build", "--no-cache"]).exit_code == ExitCode.INFRA
    assert site.services.executors[-1].runs("gen") == 1

    bad = tmp_path / "bad"
    write_site(bad, config_extra="[retry]\nmax_retries = -1\n")
    site = Site(bad, TestServices(bad))
    result = site.invoke(["build"])
    assert result.exit_code == ExitCode.USAGE, result.output
    assert "max_retries" in result.output
    assert site.services.executors == []


# P2-08: SLURM action jobs (run with --export=NONE) get what the runner needs to find itself,
# its config and the metadata service, including a bearer token, and nothing else.
def test_slurm_executor_job_env(site: Site, tmp_path: Path) -> None:
    services = site.services
    services.environ.update(
        {"PATH": "/usr/bin", TOKEN_ENV: "secret-token", "UNRELATED": "x", "HOME": "/h"}
    )
    with SiteServices.open_executor(
        services, "slurm", domain="test", log_dir=tmp_path, project="demo"
    ) as ex:
        assert isinstance(ex, SlurmExecutor)
        assert ex._env == {
            "PATH": "/usr/bin",
            "EBS_CONFIG": str(tmp_path / "config.toml"),
            TOKEN_ENV: "secret-token",
        }


class _StubSbatch:
    """Stands in for `SlurmCli`: records each submission instead of calling sbatch."""

    def __init__(self) -> None:
        self.submitted: list[tuple[Path, Any]] = []

    def submit(self, script: Path, opts: Any) -> str:
        self.submitted.append((script, opts))
        return str(4000 + len(self.submitted))


def _detach(site: Site, *flags: str) -> tuple[dict[str, Any], _StubSbatch]:
    stub = _StubSbatch()
    site.services.slurm_cli = lambda: stub  # type: ignore[assignment,method-assign,return-value]
    result = site.invoke(["build", "--detach", "--executor", "slurm", "--json", *flags])
    assert result.exit_code == ExitCode.OK, result.output
    doc: dict[str, Any] = json.loads(result.stdout.splitlines()[-1])
    return doc, stub


# P2-08 R1: the detached build records the cache mode it was asked for (the driver job runs
# `--resume-into` without --cache and uses the build's), and forwards -k / --rerun-failed.
def test_detach_records_cache_mode_and_flags(site: Site) -> None:
    doc, stub = _detach(site, "--no-cache")
    assert doc["status"] == "running"
    assert doc["driver_job"] == "4001"
    store = site.services.memory_store
    assert store.get_build(BuildId(doc["build"])).cache_mode == "off"
    script = stub.submitted[0][0].read_text()
    assert f"--resume-into {doc['uuid']}" in script
    assert "--keep-going" not in script
    assert "--rerun-failed" not in script
    assert site.services.executors == []  # nothing ran here
    doc, stub = _detach(site, "--cache", "write", "-k", "--rerun-failed")
    assert store.get_build(BuildId(doc["build"])).cache_mode == "write"
    script = stub.submitted[0][0].read_text()
    assert "--keep-going" in script
    assert "--rerun-failed" in script
    _, opts = stub.submitted[0]
    assert opts.cpus_per_task == 1
    assert opts.comment == doc["uuid"]


class _Captured(Exception):
    pass


# Review: once the driver job is queued, a build record that cannot be updated only warns; it
# never marks the build infra_failed (its driver would then find it finished and give up).
def test_detach_record_failure_does_not_fail_the_build(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(directory: Path, job: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("ebs.cli.build._record_driver_job", broken)
    stub = _StubSbatch()
    site.services.slurm_cli = lambda: stub  # type: ignore[assignment,method-assign,return-value]
    result = site.invoke(["build", "--detach", "--executor", "slurm", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    assert "could not name it" in result.stderr
    doc = json.loads(result.stdout.splitlines()[-1])
    assert site.services.memory_store.get_build(BuildId(doc["build"])).status == "running"


# Review: the driver job can start (and record its host, pid and job id) before `--detach`
# returns from sbatch; the detaching process must not overwrite that record.
def test_detach_keeps_the_record_of_a_started_driver(site: Site) -> None:
    class _StartsAtOnce(_StubSbatch):
        def submit(self, script: Path, opts: Any) -> str:
            job = super().submit(script, opts)
            directory = script.parent
            record = BuildRecord.read(directory)
            record.model_copy(update={"host": "node-a", "pid": 7, "driver_job": job}).write(
                directory
            )
            return job

    stub = _StartsAtOnce()
    site.services.slurm_cli = lambda: stub  # type: ignore[assignment,method-assign,return-value]
    result = site.invoke(["build", "--detach", "--executor", "slurm", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    uuid = json.loads(result.stdout.splitlines()[-1])["uuid"]
    status = json.loads(site.invoke(["status", uuid, "--json"]).stdout)
    assert status["driver"] == {"job": "4001", "host": "node-a", "pid": 7}


# P2-08 follow-up: `ebs status` names the driver of a detached build: its SLURM job until the
# job starts, then the job, node and pid of the driver that resumed the build there.
def test_status_names_the_driver(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    doc, _ = _detach(site)
    text = site.invoke(["status", doc["uuid"]])
    assert "driver: SLURM job 4001, not started yet (`scancel 4001` cancels)" in text.stdout
    status = json.loads(site.invoke(["status", doc["uuid"], "--json"]).stdout)
    assert status["driver"] == {"job": "4001", "host": None, "pid": None}

    site.services.environ["SLURM_JOB_ID"] = "4001"  # the driver job resumes the build
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> Any:
        status = json.loads(site.invoke(["status", doc["uuid"], "--json"]).stdout)
        seen.update(status)
        text = site.invoke(["status", doc["uuid"]]).stdout
        seen["text"] = text
        raise _Captured

    monkeypatch.setattr("ebs.cli.build.resume_build", capture)
    site.invoke(["build", "--resume-into", doc["uuid"], "--executor", "local"])
    host, pid = socket.gethostname(), os.getpid()
    assert seen["driver"] == {"job": "4001", "host": host, "pid": pid}
    assert f"driver: SLURM job 4001 on {host} (pid {pid})" in seen["text"]


# P2-08 R4: both CLI driver paths handle SIGTERM, which `scancel` of a driver job sends.
@pytest.mark.parametrize("entry", ["run_build", "resume_build"])
def test_cli_drivers_handle_sigterm(
    site: Site, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    args = ["build"]
    if entry == "resume_build":
        first = site.invoke(["build", "--json"])
        assert first.exit_code == ExitCode.OK, first.output
        args += ["--resume-into", json.loads(first.stdout.splitlines()[-1])["uuid"]]
    seen: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise _Captured

    monkeypatch.setattr(f"ebs.cli.build.{entry}", capture)
    site.invoke(args)
    assert seen["handle_sigterm"] is True
    assert seen["handle_sigint"] is True
