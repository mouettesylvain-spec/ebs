"""Driver events (interfaces.md § 10): to the MetadataStore and to a local JSONL file.

`.ebs/builds/<uuid>/events.jsonl` lets `ebs status` / `ebs logs` work when the metadata service
is unreachable. Each line is one `Event` in its JSON form. The file is written first, so a
store failure still leaves a local trace.
"""

from __future__ import annotations

import json
from pathlib import Path

from ebs.core.clock import Clock
from ebs.meta.api import BuildId, Event, EventType, MetadataStore

__all__ = ["Event", "EventLog", "read_events"]


class EventLog:
    def __init__(self, store: MetadataStore, build: BuildId, path: Path, clock: Clock) -> None:
        self._store = store
        self._build = build
        self._clock = clock
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, type: EventType, action_id: str | None = None, **data: object) -> Event:
        """Record one event; `data` values must be canonical JSON (no floats)."""
        event = Event(
            ts=self._clock.now(), build=self._build, type=type, action_id=action_id, data=data
        )
        line = json.dumps(event.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        self._store.emit(event)
        return event


def read_events(path: Path) -> list[Event]:
    """The events of a local `events.jsonl` file, in order (for `ebs status` / `ebs logs`)."""
    with path.open(encoding="utf-8") as f:
        return [Event.model_validate_json(line) for line in f if line.strip()]
