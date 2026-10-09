from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import timedelta
from uuid import UUID

from ebs.core.errors import ExitCode
from tests.helpers.cli import T0, Site, append_event, write_build

U1 = UUID("11111111-1111-4111-8111-111111111111")
U2 = UUID("22222222-2222-4222-8222-222222222222")
ACTIONS = {
    "gen": "gen",
    "sim[test=a]": "sim",
    "sim[test=b]": "sim",
    "sim[test=c]": "sim",
    "sim[test=d]": "sim",
}
EVENTS = [
    ("cache_hit", "gen", {"key": "sha256:" + "0" * 64, "status": "passed"}),
    ("submitted", "sim[test=a]", {"job_id": "1", "attempt": 1, "batch_size": 3}),
    ("submitted", "sim[test=b]", {"job_id": "2", "attempt": 1, "batch_size": 3}),
    ("submitted", "sim[test=c]", {"job_id": "3", "attempt": 1, "batch_size": 3}),
    ("pending", "sim[test=a]", {"reason": "licenses"}),
    ("pending", "sim[test=b]", {"reason": "resources"}),
    ("pending", "sim[test=c]", {"reason": "licenses"}),
    ("running", "sim[test=c]", {"job_id": "3"}),
]


# R3
def test_pending_reasons_split(site: Site) -> None:
    write_build(site.proj, U1, ACTIONS, EVENTS)
    result = site.invoke(["status"])
    assert result.exit_code == ExitCode.OK, result.output
    out = result.stdout
    assert str(U1) in out
    assert "pending: licenses" in out
    assert "pending: resources" in out

    doc = json.loads(site.invoke(["status", "--json"]).stdout)
    assert doc["uuid"] == str(U1)
    assert doc["build"] == 7
    assert doc["status"] == "running"
    steps = {s["step"]: s for s in doc["steps"]}
    assert steps["gen"]["states"] == {"cached": 1}
    assert steps["sim"]["states"] == {
        "pending: licenses": 1,
        "pending: resources": 1,
        "running": 1,
        "queued": 1,
    }
    assert doc["totals"] == {
        "cached": 1,
        "pending: licenses": 1,
        "pending: resources": 1,
        "queued": 1,
        "running": 1,
    }


# R3
def test_final_states_and_retry(site: Site) -> None:
    write_build(
        site.proj,
        U1,
        {"gen": "gen", "sim[test=a]": "sim"},
        [
            ("submitted", "gen", {"job_id": "1", "attempt": 1, "batch_size": 1}),
            ("infra_failed", "gen", {"reason": "oom"}),
            ("retrying", "gen", {"attempt": 2, "delay_ms": 1000}),
            ("submitted", "gen", {"job_id": "2", "attempt": 2, "batch_size": 1}),
            ("finished", "gen", {"state": "done", "exit_code": 0, "attempts": 2}),
            ("finished", "sim[test=a]", {"state": "skipped", "reason": "producer failed"}),
            ("build_finished", None, {"status": "failed", "counts": {"done": 1, "skipped": 1}}),
        ],
    )
    doc = json.loads(site.invoke(["status", "--json"]).stdout)
    assert doc["status"] == "failed"
    assert doc["totals"] == {"done": 1, "skipped": 1}


# R3
def test_default_is_last_build_and_prefix_selects(site: Site) -> None:
    write_build(site.proj, U1, ACTIONS, EVENTS)
    write_build(site.proj, U2, {"gen": "gen"}, [], build=8, start=T0.replace(hour=10))
    assert json.loads(site.invoke(["status", "--json"]).stdout)["uuid"] == str(U2)
    assert json.loads(site.invoke(["status", "--json", "1111"]).stdout)["uuid"] == str(U1)
    assert json.loads(site.invoke(["status", "--json", "7"]).stdout)["uuid"] == str(U1)


# R3
def test_no_builds_is_usage_error(site: Site) -> None:
    result = site.invoke(["status"])
    assert result.exit_code == ExitCode.USAGE
    assert "ebs build" in result.stderr


# R3
def test_watch_refreshes_until_finished(site: Site) -> None:
    path = write_build(site.proj, U1, ACTIONS, EVENTS)
    clock = site.services.clock
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            append_event(path, "build_finished", None, {"status": "passed", "counts": {}})

    clock.sleep = sleep  # type: ignore[method-assign]
    result = site.invoke(["status", "--watch", "--interval", "2"])
    assert result.exit_code == ExitCode.OK, result.output
    assert sleeps == [2.0, 2.0]
    assert len(re.findall(rf"build {U1}", result.stdout)) == 3  # one view per refresh
    assert "passed" in result.stdout


def _uuid(site: Site, *args: str) -> str:
    result = site.invoke(["status", "--json", *args])
    assert result.exit_code == ExitCode.OK, result.output
    return str(json.loads(result.stdout)["uuid"])


# R3 (test-critic: "latest" is by start time, not by UUID or directory order)
def test_latest_is_by_time_not_uuid(site: Site) -> None:
    write_build(site.proj, U2, {"gen": "gen"}, [], build=1, start=T0 + timedelta(hours=1))
    write_build(site.proj, U1, {"gen": "gen"}, [], build=2, start=T0 + timedelta(hours=2))
    last = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")  # sorts last, started first
    write_build(site.proj, last, {"gen": "gen"}, [], build=3, start=T0)
    assert _uuid(site) == str(U1)


# R3 (test-critic: a number names a build id before a UUID prefix)
def test_numeric_ref_prefers_build_id(site: Site) -> None:
    sevens = UUID("77777777-7777-4777-8777-777777777777")
    write_build(site.proj, U1, {"gen": "gen"}, [], build=7)
    write_build(site.proj, sevens, {"gen": "gen"}, [], build=8, start=T0 + timedelta(hours=1))
    assert _uuid(site, "7") == str(U1)
    assert _uuid(site, "7777") == str(sevens)  # no build 7777: a UUID prefix


# R3 (test-critic: an ambiguous prefix is an error, any case)
def test_ambiguous_build_prefix_is_usage_error(site: Site) -> None:
    a = UUID("abcd1111-1111-4111-8111-111111111111")
    b = UUID("abcd2222-2222-4222-8222-222222222222")
    write_build(site.proj, a, {"gen": "gen"}, [], build=1)
    write_build(site.proj, b, {"gen": "gen"}, [], build=2, start=T0 + timedelta(hours=1))
    result = site.invoke(["status", "abcd"])
    assert result.exit_code == ExitCode.USAGE
    assert str(a) in result.stderr
    assert str(b) in result.stderr
    assert _uuid(site, "ABCD2") == str(b)


# R3 (test-critic: a missing events file falls back to the record's status)
def test_status_falls_back_to_record(site: Site) -> None:
    path = write_build(site.proj, U1, {"gen": "gen"}, [], status="passed")
    (path / "events.jsonl").unlink()
    assert json.loads(site.invoke(["status", "--json"]).stdout)["status"] == "passed"
    site.services.clock.sleep = _no_sleep  # type: ignore[method-assign]
    assert site.invoke(["status", "--watch"]).exit_code == ExitCode.OK


def _no_sleep(seconds: float) -> None:
    raise AssertionError("status --watch must not wait on a finished build")


# R3 (review: a driver killed without a trace must not keep --watch waiting forever)
def test_dead_driver_is_interrupted(site: Site) -> None:
    gone = _exited_pid()
    write_build(site.proj, U1, ACTIONS, EVENTS, host=socket.gethostname(), pid=gone)
    site.services.clock.sleep = _no_sleep  # type: ignore[method-assign]
    result = site.invoke(["status", "--watch", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    assert json.loads(result.stdout)["status"] == "interrupted"


# R3
def test_live_or_remote_driver_stays_running(site: Site) -> None:
    write_build(site.proj, U1, ACTIONS, EVENTS, host=socket.gethostname(), pid=os.getpid())
    assert json.loads(site.invoke(["status", "--json"]).stdout)["status"] == "running"
    write_build(site.proj, U2, ACTIONS, EVENTS, host="another-host", pid=_exited_pid(), build=8,
                start=T0 + timedelta(hours=1))  # fmt: skip
    assert json.loads(site.invoke(["status", "--json"]).stdout)["status"] == "running"


def _exited_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid
