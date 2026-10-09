from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from ebs.cas.fs import FsCAS
from ebs.cli._records import BuildRecord
from ebs.core.digest import hash_bytes
from ebs.core.errors import ExitCode
from ebs.driver.events import Event
from ebs.meta.api import ActionRow, BuildCreate, BuildId
from ebs.plan.types import ActionSpec
from ebs.runner.run import _CappedLog
from ebs.runner.stage import action_dir_name
from tests.helpers.cli import T0, Site, append_event, write_build
from tests.helpers.driver import Node, make_plan, result_for

U1 = UUID("11111111-1111-4111-8111-111111111111")
ACTIONS = {"gen": "gen", "sim[test=smoke]": "sim", "sim[test=random]": "sim"}


def _store_build(site: Site) -> int:
    """A build of ACTIONS in the metadata store, nothing recorded yet; returns its id."""
    store = site.services.memory_store
    build = store.create_build(
        BuildCreate(
            uuid=U1, domain="test", project="demo", plan_digest=hash_bytes(b"p"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice", cache_mode="read",
        )
    )  # fmt: skip
    store.add_actions(build, [ActionRow(action_id=a, step=s) for a, s in ACTIONS.items()])
    return int(build)


def _finished_build(site: Site) -> BuildId:
    """A build in the store whose `gen` finished with a log blob in the CAS."""
    store = site.services.memory_store
    cas = FsCAS(site.root / "cas", "test", clock=site.services.clock)
    plan = make_plan(cas, {"gen": Node()})
    spec: ActionSpec = plan.actions[0]
    build = store.create_build(
        BuildCreate(
            uuid=U1, domain="test", project="demo", plan_digest=cas.put_bytes(b"p"),
            flow_repo="", flow_commit="", flow_dirty=False, user_name="alice", cache_mode="read",
        )
    )  # fmt: skip
    store.add_actions(build, [ActionRow(action_id="gen", step="gen", key=spec.key)])
    log = cas.put_bytes(b"line 1\nline 2: gen finished\n")
    result = result_for(spec).model_copy(update={"log": log})
    store.record_result(build, "gen", result)
    write_build(
        site.proj,
        U1,
        ACTIONS,
        [("finished", "gen", {"state": "done", "exit_code": 0})],
        build=int(build),
    )
    return build


# R4
def test_prefix_match(site: Site) -> None:
    _finished_build(site)
    result = site.invoke(["logs", "ge"])  # unique prefix of "gen"
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "line 1\nline 2: gen finished\n"

    ambiguous = site.invoke(["logs", "sim"])
    assert ambiguous.exit_code == ExitCode.USAGE
    assert "sim[test=random]" in ambiguous.stderr
    assert "sim[test=smoke]" in ambiguous.stderr

    unknown = site.invoke(["logs", "nope"])
    assert unknown.exit_code == ExitCode.USAGE
    assert "nope" in unknown.stderr


# R4
def test_exact_id_wins_over_prefix(site: Site) -> None:
    write_build(site.proj, U1, {"gen": "gen", "gen2": "gen2"}, [])
    result = site.invoke(["logs", "gen"])
    assert "matches several" not in result.stderr  # `gen` is exact; it has no log yet
    assert "has not started" in result.stderr


# R4
def test_json_mode(site: Site) -> None:
    _finished_build(site)
    doc = json.loads(site.invoke(["logs", "gen", "--json"]).stdout)
    assert doc["action_id"] == "gen"
    assert doc["state"] == "done"
    assert doc["log"] == "line 1\nline 2: gen finished\n"


def _logs_dir(site: Site, build: int, action_id: str) -> Path:
    """The runner's scratch logs dir for the action (ebs.runner.stage.Scratch layout)."""
    return site.root / "scratch" / str(build) / action_dir_name(action_id) / "logs"


def _runner_log(site: Site, build: int, action_id: str, *, cap: int = 1 << 20) -> _CappedLog:
    """The runner's own log writer: `tool.log.cur` (+ `.prev`) while running, `tool.log` at end."""
    logs = _logs_dir(site, build, action_id)
    logs.mkdir(parents=True)
    return _CappedLog(logs / "tool.log", cap)


def _on_sleeps(site: Site, *steps: Callable[[], object]) -> list[float]:
    """Make the n-th `clock.sleep` of the command run `steps[n]` (what happens meanwhile)."""
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        steps[len(sleeps) - 1]()

    site.services.clock.sleep = sleep  # type: ignore[method-assign]
    return sleeps


def _finish(build: Path, action_id: str = "gen") -> None:
    append_event(build, "finished", action_id, {"state": "done", "exit_code": 0})


# R4
def test_running_prints_live_log(site: Site) -> None:
    write_build(site.proj, U1, ACTIONS, [("running", "gen", {"job_id": "1"})])
    log = _runner_log(site, 7, "gen", cap=8)
    log.write(b"aaaaaaa\nbbb\n")  # rotated once: .prev + .cur
    result = site.invoke(["logs", "gen"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "aaaaaaa\nbbb\n"
    log.close()


# R4
def test_follow(site: Site) -> None:
    build = write_build(site.proj, U1, ACTIONS, [("running", "gen", {"job_id": "1"})])
    log = _runner_log(site, 7, "gen")
    log.write(b"first\n")

    def last_line_and_finish() -> None:
        log.write(b"third\n")
        log.close()  # .cur becomes tool.log
        _finish(build)

    sleeps = _on_sleeps(
        site,
        lambda: log.write(b"second\n"),
        lambda: None,  # nothing new: keep waiting
        last_line_and_finish,
    )
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "first\nsecond\nthird\n"  # every byte once, in order
    assert len(sleeps) == 3


# R4
def test_follow_across_rotation(site: Site) -> None:
    build = write_build(site.proj, U1, ACTIONS, [("running", "gen", {"job_id": "1"})])
    log = _runner_log(site, 7, "gen", cap=8)
    log.write(b"1234567\n")  # .cur is full

    def finish() -> None:
        log.write(b"xyz\n")
        log.close()  # joins .prev and .cur into tool.log, deletes both
        _finish(build)

    # .cur becomes .prev and a new .cur holds "ABC"; the end comes in a later interval
    _on_sleeps(site, lambda: log.write(b"ABC"), finish)
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "1234567\nABCxyz\n"


# R4 (review: a retry recreates the scratch dir)
def test_follow_across_retry(site: Site) -> None:
    build = write_build(site.proj, U1, ACTIONS, [("running", "gen", {"job_id": "1"})])
    first = _runner_log(site, 7, "gen")
    first.write(b"attempt one, a long line\n")
    second: list[_CappedLog] = []

    def retry() -> None:
        first.write(b"died\n")
        shutil.rmtree(_logs_dir(site, 7, "gen").parent)  # what Scratch.create does
        second.append(_runner_log(site, 7, "gen"))
        second[0].write(b"two\n")

    def finish() -> None:
        second[0].close()
        _finish(build)

    _on_sleeps(site, retry, finish)
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == (
        "attempt one, a long line\ndied\n--- ebs: gen restarted (new attempt) ---\ntwo\n"
    )


# R4
def test_follow_waits_for_start(site: Site) -> None:
    build = write_build(site.proj, U1, ACTIONS, [])
    logs: list[_CappedLog] = []

    def start() -> None:
        append_event(build, "running", "gen", {"job_id": "1"})
        logs.append(_runner_log(site, 7, "gen"))
        logs[0].write(b"out\n")

    def finish() -> None:
        logs[0].close()
        _finish(build)

    _on_sleeps(site, start, finish)
    result = site.invoke(["logs", "gen", "--follow"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "out\n"


# R4 (test-critic: bytes written just before the final event must not be lost)
def test_follow_reads_after_observing_final_state(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    build = write_build(site.proj, U1, ACTIONS, [("running", "gen", {"job_id": "1"})])
    log = _runner_log(site, 7, "gen")
    log.write(b"head\n")
    calls = 0
    real = BuildRecord.events

    def events(record: BuildRecord, workdir: Path) -> list[Event]:
        nonlocal calls
        calls += 1
        if calls == 2:  # the tool ends between two polls
            log.write(b"tail\n")
            log.close()
            _finish(build)
        return real(record, workdir)

    monkeypatch.setattr(BuildRecord, "events", events)
    _on_sleeps(site, lambda: None)
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "head\ntail\n"


# R4 (test-critic: an action that never finishes must not hang `-f` once the build ended)
def test_follow_stops_when_build_finishes(site: Site) -> None:
    build = write_build(
        site.proj,
        U1,
        ACTIONS,
        [("submitted", "gen", {"job_id": "1", "attempt": 1})],
        build=_store_build(site),
    )
    sleeps = _on_sleeps(
        site,
        lambda: append_event(build, "build_finished", None, {"status": "cancelled", "counts": {}}),
    )
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert len(sleeps) == 1


# R4
def test_follow_after_finish_prints_stored_log(site: Site) -> None:
    _finished_build(site)  # finished, scratch already removed
    sleeps = _on_sleeps(site)
    result = site.invoke(["logs", "gen", "-f"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "line 1\nline 2: gen finished\n"
    assert sleeps == []


# R4 (test-critic: an infra failure's log comes from its event)
def test_infra_failed_shows_failure_log(site: Site) -> None:
    log = FsCAS(site.root / "cas", "test").put_bytes(b"license timeout\n")
    write_build(
        site.proj,
        U1,
        ACTIONS,
        [
            ("submitted", "gen", {"job_id": "1", "attempt": 1}),
            ("infra_failed", "gen", {"reason": "license", "log": str(log)}),
        ],
        build=_store_build(site),  # no result recorded: the log comes from the event
    )
    result = site.invoke(["logs", "gen"])
    assert result.exit_code == ExitCode.OK, result.output
    assert result.stdout == "license timeout\n"


# R4 R6
def test_follow_with_json_is_usage_error(site: Site) -> None:
    _finished_build(site)
    result = site.invoke(["logs", "gen", "-f", "--json"])
    assert result.exit_code == ExitCode.USAGE
    assert "--json" in result.stderr


# R4
def test_build_option_selects_build(site: Site) -> None:
    _finished_build(site)
    other = UUID("33333333-3333-4333-8333-333333333333")
    write_build(site.proj, other, {"x": "x"}, [], build=99, start=T0 + timedelta(hours=2))
    assert site.invoke(["logs", "gen"]).exit_code == ExitCode.USAGE  # last build has no gen
    result = site.invoke(["logs", "gen", "--build", "1111"])
    assert result.exit_code == ExitCode.OK, result.output
    assert "gen finished" in result.stdout
