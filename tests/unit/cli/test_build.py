from __future__ import annotations

import json
import os
import re
import signal
from typing import Any

import pytest

from ebs.cli._context import EXECUTORS
from ebs.cli.build import CacheChoice, ExecutorChoice
from ebs.core.errors import ExitCode
from ebs.driver.cache_policy import CACHE_MODES
from ebs.driver.events import read_events
from tests.helpers.cli import Site


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
        pytest.param(["--executor", "slurm"], id="unknown-executor"),
    ],
)
def test_usage_errors(site: Site, args: list[str]) -> None:
    result = site.invoke(["build", *args])
    assert result.exit_code == ExitCode.USAGE, result.output


# R2
def test_build_needs_metadata_store(site: Site) -> None:
    site.services.with_store = False
    result = site.invoke(["build"])
    assert result.exit_code == ExitCode.USAGE
    assert "[metadata].url" in result.stderr
