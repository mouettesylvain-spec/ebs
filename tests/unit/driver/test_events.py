from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs.core.errors import MetadataError
from ebs.driver.events import Event, EventLog, read_events
from ebs.meta.api import BuildCreate
from ebs.meta.memory import InMemoryMetadataStore
from tests.helpers.driver import Env, Node


class RecordingStore(InMemoryMetadataStore):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.emitted: list[Event] = []

    def emit(self, event: Event) -> None:
        super().emit(event)
        self.emitted.append(event)


def _lines(path: Path) -> list[Event]:
    events = read_events(path)
    assert len(events) == len(path.read_text().splitlines())  # one event per line
    for line in path.read_text().splitlines():
        assert json.loads(line)["v"] == 1
    return events


# R8
def test_jsonl_and_store(env: Env) -> None:
    store = RecordingStore(clock=env.clock)
    env.store = store
    executor = env.executor()
    executor.script("a", ("infra", "preempted"), "pass", pending_polls=1)
    outcome = env.run(env.plan({"a": Node(), "b": Node(deps=(("a", "out"),))}), executor)

    assert (
        outcome.events_path == env.workdir / ".ebs" / "builds" / str(outcome.uuid) / "events.jsonl"
    )
    on_disk = _lines(outcome.events_path)
    assert on_disk == store.emitted  # same events, same order
    assert all(e.build == outcome.build for e in on_disk)
    types = [(e.type, e.action_id) for e in on_disk]
    assert types[:2] == [("build_started", None), ("plan_ready", None)]
    assert types[-1] == ("build_finished", None)
    for expected in [
        ("submitted", "a"),
        ("pending", "a"),
        ("infra_failed", "a"),
        ("retrying", "a"),
        ("running", "a"),
        ("finished", "a"),
        ("submitted", "b"),
        ("finished", "b"),
    ]:
        assert expected in types
    assert types.index(("infra_failed", "a")) < types.index(("retrying", "a"))
    assert types.index(("finished", "a")) < types.index(("submitted", "b"))
    finished = on_disk[-1]
    assert finished.data["status"] == "passed"
    assert finished.data["counts"] == {"done": 2}


# R8
def test_event_log_appends(tmp_path: Path, env: Env) -> None:
    store = RecordingStore(clock=env.clock)
    build = store.create_build(_build_create())
    path = tmp_path / "deep" / "events.jsonl"
    log = EventLog(store, build, path, env.clock)
    log.emit("cache_hit", action_id="a", key="sha256:" + "1" * 64)
    log.emit("build_finished", status="passed")
    events = _lines(path)
    assert [(e.type, e.action_id, e.data) for e in events] == [
        ("cache_hit", "a", {"key": "sha256:" + "1" * 64}),
        ("build_finished", None, {"status": "passed"}),
    ]
    assert all(e.ts == env.clock.now() for e in events)
    assert store.emitted == events


class FailingStore(InMemoryMetadataStore):
    def emit(self, event: Event) -> None:
        raise MetadataError("metadata service unreachable")


# R8: the local file is written first, so a store outage still leaves a trace.
def test_file_written_before_store(tmp_path: Path, env: Env) -> None:
    store = FailingStore(clock=env.clock)
    build = store.create_build(_build_create())
    path = tmp_path / "events.jsonl"
    with pytest.raises(MetadataError):
        EventLog(store, build, path, env.clock).emit("cache_hit", action_id="a")
    assert [(e.type, e.action_id) for e in read_events(path)] == [("cache_hit", "a")]


# R4 (P2-06): a pending event names the action's license features, so the dashboard splits
# license waits by feature.
def test_pending_event_names_license_features(env: Env) -> None:
    executor = env.executor()
    executor.script("a", "pass", pending_polls=1)
    executor.script("b", "pass", pending_polls=1)
    plan = env.plan({"a": Node(licenses={"simfeat": 1, "addon": 2}), "b": Node()})
    outcome = env.run(plan, executor)

    events = read_events(outcome.events_path)
    pending = {e.action_id: e.data for e in events if e.type == "pending"}
    assert pending["a"] == {"reason": "licenses", "licenses": ["addon", "simfeat"]}
    assert pending["b"] == {"reason": "licenses"}  # no licenses: no field


# R1 (P2-06): plan_ready carries the step graph, from which the dashboard draws the build's DAG.
def test_plan_ready_has_the_step_graph(env: Env) -> None:
    nodes = {
        "compile[a]": Node(),
        "compile[b]": Node(deps=(("compile[a]", "out"),)),  # within a step: no step edge
        "elab": Node(deps=(("compile[a]", "out"), ("compile[b]", "out"))),
        "sim[1]": Node(deps=(("elab", "out"),)),
        "lint": Node(),
        "report": Node(deps=(("sim[1]", "out"), ("lint", "out"))),  # two upstream steps
    }
    outcome = env.run(env.plan(nodes), env.executor())
    ready = next(e for e in read_events(outcome.events_path) if e.type == "plan_ready")
    assert ready.data["actions"] == 6
    assert ready.data["steps"] == {
        "compile": [], "elab": ["compile"], "lint": [], "report": ["lint", "sim"], "sim": ["elab"],
    }  # fmt: skip


def _build_create() -> BuildCreate:
    return BuildCreate.model_validate_json(
        json.dumps(
            {
                "uuid": "00000000-0000-0000-0000-0000000000aa",
                "domain": "test",
                "project": "demo",
                "plan_digest": "sha256:" + "0" * 64,
                "flow_repo": "",
                "flow_commit": "",
                "flow_dirty": False,
                "user_name": "alice",
                "cache_mode": "read",
            }
        )
    )
