"""Local build records: `.ebs/builds/<uuid>/build.json` next to the driver's `events.jsonl`.

`ebs build` writes `build.json` when the build starts and rewrites it when it ends; `ebs status`,
`ebs logs` and `ebs plan` (baseline) read it, so they work without the metadata store
(interfaces.md § 14). Action states are folded from the events.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError

from ebs.core.errors import ConfigError
from ebs.driver.build import events_path
from ebs.driver.events import Event, read_events
from ebs.meta.api import BuildId, BuildStatus, DigestField

__all__ = [
    "RECORD_FILE",
    "BuildRecord",
    "action_states",
    "build_dir",
    "build_status",
    "find_build",
    "list_builds",
]

RECORD_FILE: Final = "build.json"
FINAL_STATES: Final = frozenset({"done", "failed", "cached", "skipped", "cancelled"})


class BuildRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1
    uuid: UUID
    build: BuildId
    domain: str
    project: str
    flow: str  # absolute path of the flow file
    plan: DigestField  # plan.json as submitted
    final_plan: DigestField | None  # as refined at the end (every key that could be computed)
    status: BuildStatus
    actions: dict[str, str]  # action_id -> step, plan order
    created_at: AwareDatetime
    host: str = ""  # where the driver (`ebs build`) runs; "" = unknown
    pid: int = 0  # the driver's process id; 0 = unknown

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{RECORD_FILE}.tmp"
        tmp.write_text(self.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, directory / RECORD_FILE)

    @classmethod
    def read(cls, directory: Path) -> BuildRecord:
        path = directory / RECORD_FILE
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ConfigError(f"cannot read the build record {path}: {exc}") from None

    def events(self, workdir: Path) -> list[Event]:
        path = events_path(workdir, self.uuid)
        return read_events(path) if path.exists() else []


def build_dir(workdir: Path, uuid: UUID) -> Path:
    return events_path(workdir, uuid).parent


def list_builds(workdir: Path) -> list[BuildRecord]:
    """The builds recorded under `workdir`, oldest first; unreadable records are skipped."""
    root = workdir / ".ebs" / "builds"
    records = []
    for entry in sorted(root.iterdir()) if root.is_dir() else []:
        if (entry / RECORD_FILE).is_file():
            try:
                records.append(BuildRecord.read(entry))
            except ConfigError:
                continue
    return sorted(records, key=lambda r: (r.created_at, str(r.uuid)))


def find_build(workdir: Path, ref: str | None, *, flow: Path | None = None) -> BuildRecord:
    """`ref`: a UUID (or unique prefix) or a build id; None = the latest build (of `flow`)."""
    builds = [b for b in list_builds(workdir) if flow is None or Path(b.flow) == flow]
    where = workdir / ".ebs" / "builds"
    if not builds:
        named = f" (looking for build {ref!r})" if ref is not None else ""
        raise ConfigError(f"no builds recorded in {where}{named}; run `ebs build` first")
    if ref is None:
        return builds[-1]
    by_id = [b for b in builds if ref.isdigit() and b.build == int(ref)]
    # An id wins; digits that name no id may still begin a UUID.
    matches = by_id or [b for b in builds if str(b.uuid).startswith(ref.lower())]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ConfigError(f"no build {ref!r} in {where}; give a build UUID (or prefix) or id")
    names = ", ".join(str(b.uuid) for b in matches)
    raise ConfigError(f"build {ref!r} matches several builds ({names}); give more of the UUID")


def _state(event: Event) -> str | None:
    match event.type:
        case "cache_hit":
            return "cached"
        case "submitted":
            return "submitted"
        case "pending":
            return f"pending: {event.data.get('reason', 'other')}"
        case "running":
            return "running"
        case "infra_failed":
            return "infra_failed"
        case "retrying":
            return "retrying"
        case "finished":
            return str(event.data.get("state", "done"))
        case _:
            return None


def action_states(actions: Iterable[str], events: Sequence[Event]) -> dict[str, str]:
    """Latest state per action (plan order): `queued` until an event says otherwise."""
    states = dict.fromkeys(actions, "queued")
    for event in events:
        state = _state(event)
        if state is not None and event.action_id is not None:
            states[event.action_id] = state
    return states


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def build_status(
    record: BuildRecord,
    events: Sequence[Event],
    *,
    alive: Callable[[int], bool] = pid_alive,
) -> str:
    """The `build_finished` status, else the record's; `interrupted` for a `running` build
    whose driver died on this host (killed, rebooted), so `--watch` and `-f` do not wait forever.
    """
    for event in reversed(events):
        if event.type == "build_finished":
            return str(event.data.get("status", record.status))
    if (
        record.status == "running"
        and record.pid > 0
        and record.host == socket.gethostname()
        and not alive(record.pid)
    ):
        return "interrupted"
    return record.status
