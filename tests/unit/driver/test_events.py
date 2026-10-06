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
