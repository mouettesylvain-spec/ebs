"""Executor test helpers: a scripted fake runner and batches of hand-built actions."""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ebs.core.digest import hash_bytes
from ebs.exec.api import Executor, JobHandle, JobStatus, SubmitBatch
from ebs.flow.model import Resources
from ebs.meta.api import BuildId
from ebs.plan.types import ActionSpec
from tests.helpers import fake_runner
from tests.helpers.runner import DOMAIN, shell_spec
from tests.helpers.wait import wait_until

PLAN = hash_bytes(b"plan")
TERMINAL = {"done", "infra_failed", "cancelled"}


@dataclass
class FakeRunner:
    """Drives `tests/helpers/fake_runner.py`: what each action does, and what it recorded."""

    root: Path
    script: dict[str, dict[str, object]] = field(default_factory=dict)

    @property
    def record(self) -> Path:
        return self.root / "record"

    @property
    def argv(self) -> tuple[str, ...]:
        return (sys.executable, str(fake_runner.PATH))

    def env(self) -> dict[str, str]:
        self.root.mkdir(parents=True, exist_ok=True)
        script = self.root / "script.json"
        script.write_text(json.dumps(self.script))
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            fake_runner.SCRIPT_ENV: str(script),
            fake_runner.RECORD_ENV: str(self.record),
        }

    def set(self, action_id: str, **behaviour: object) -> None:
        """Script `action_id`; must be called before the executor is created (env is fixed)."""
        self.script[action_id] = behaviour

    def release(self, action_id: str) -> None:
        release = self.record / "release"
        release.mkdir(parents=True, exist_ok=True)
        (release / fake_runner.record_name(action_id)).write_text("go")

    def started(self) -> list[str]:
        return [str(s["action"]) for s in self.starts()]

    def starts(self) -> list[dict[str, object]]:
        path = self.record / "starts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def argv_of(self, action_id: str) -> list[str]:
        path = self.record / "argv" / f"{fake_runner.record_name(action_id)}.json"
        data: list[str] = json.loads(path.read_text())
        return data

    def cleaned(self, action_id: str) -> bool:
        return (self.record / "cleaned" / fake_runner.record_name(action_id)).exists()

    def running(self) -> bool:
        path = self.record / "running"
        return path.exists() and any(path.iterdir())


def spec(action_id: str, *, cpus: int | None = None, mem: str | None = None) -> ActionSpec:
    base = shell_spec("true\n", action_id=action_id)
    return dataclasses.replace(base, resources=Resources(cpus=cpus, mem=mem))


def batch(
    *action_ids: str,
    cpus: Mapping[str, int] | None = None,
    mem: str | None = None,
    build: BuildId | None = None,
) -> SubmitBatch:
    specs = tuple(spec(a, cpus=(cpus or {}).get(a), mem=mem) for a in action_ids)
    return SubmitBatch(plan=PLAN, domain=DOMAIN, build=build, actions=specs)


def wait_states(
    executor: Executor,
    handles: Sequence[JobHandle],
    *,
    until: set[str] | None = None,
    timeout: float = 10.0,
) -> dict[JobHandle, JobStatus]:
    """Poll until every handle is in `until` (default: a terminal state); the last poll."""
    wanted = until or TERMINAL
    last: dict[JobHandle, JobStatus] = {}

    def check() -> bool:
        nonlocal last
        last = executor.poll(handles)
        return all(s.state in wanted for s in last.values())

    wait_until(check, timeout=timeout, what=f"jobs in {sorted(wanted)}")
    return last
