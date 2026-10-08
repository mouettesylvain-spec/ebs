"""CLI test helpers: a flow on disk, a real FsCAS and stat cache, an in-memory store and a
ScriptedExecutor, injected through `TestServices` (`CliRunner.invoke(app, …, obj=services)`).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from typer.testing import CliRunner, Result

from ebs.cas.fs import FsCAS
from ebs.cli._context import SiteServices
from ebs.cli._records import BuildRecord, build_dir
from ebs.cli.main import app
from ebs.core.digest import hash_bytes
from ebs.exec.api import Executor
from ebs.meta.api import BuildId, Event, MetadataStore
from ebs.meta.memory import InMemoryMetadataStore
from ebs.sources.statcache import StatCache
from tests.helpers.driver import ScriptedExecutor, SleepRecorder

DOMAIN = "test"

FLOW = """\
version: 1
project: demo
domain: test
steps:
  gen:
    kind: shell
    script: "cat src/a.txt > out.txt"
    inputs: { src: "src/*.txt" }
    outputs: { out: out.txt }
  sim:
    kind: shell
    matrix: { table: tests.csv }
    command: [cat, "${steps.gen.outputs.out}"]
    params: { test: "${row.test}" }
    outputs: { result: result.txt }
"""

TESTS_CSV = "test\nsmoke\nrandom\n"


class TestServices(SiteServices):
    """SiteServices with an in-memory store, a scripted executor and a fake clock."""

    __test__ = False

    def __init__(self, root: Path, *, with_store: bool = True, tty: bool = False) -> None:
        super().__init__(
            environ={"EBS_CONFIG": str(root / "config.toml"), "USER": "alice"},
            cwd=root / "proj",
            home=root / "home",
            clock=SleepRecorder(),
            statcache_default=root / "var-tmp" / "statcache.sqlite",
        )
        self.memory_store = InMemoryMetadataStore(clock=self.clock)
        self.with_store = with_store
        self.tty = tty
        self.scripts: dict[str, tuple[Any, ...]] = {}
        self.on_poll: Callable[[int], None] | None = None
        self.executors: list[ScriptedExecutor] = []

    def store(self, *, required: bool) -> MetadataStore | None:
        if not self.with_store:
            return super().store(required=required)
        return self.memory_store

    @contextmanager
    def open_executor(self, name: str, *, domain: str, log_dir: Path) -> Iterator[Executor]:
        executor = ScriptedExecutor(self.memory_store, self.cas(domain), on_poll=self.on_poll)
        for action_id, outcomes in self.scripts.items():
            executor.script(action_id, *outcomes)
        self.executors.append(executor)
        yield executor

    def is_tty(self) -> bool:
        return self.tty


@dataclass
class Site:
    root: Path
    services: TestServices
    runner: CliRunner = field(default_factory=CliRunner)

    @property
    def proj(self) -> Path:
        return self.root / "proj"

    def invoke(self, args: Sequence[str], **kwargs: Any) -> Result:
        # Rich sizes tables from COLUMNS or the real terminal running pytest; a narrow one would
        # truncate headers ("pen…"), so pin a wide width unless the test sets its own.
        env = {"COLUMNS": "200", **kwargs.pop("env", {})}
        return self.runner.invoke(app, list(args), obj=self.services, env=env, **kwargs)


def write_site(root: Path, *, flow: str = FLOW, config_extra: str = "") -> None:
    proj = root / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "flow.yaml").write_text(flow)
    (proj / "tests.csv").write_text(TESTS_CSV)
    (proj / "src" / "a.txt").write_text("hello\n")
    (root / "config.toml").write_text(
        f'[cas]\nroot = "{root / "cas"}"\n'
        f'[scratch]\ndir = "{root / "scratch"}"\n'
        f'[stat_cache]\npath = "{root / "statcache.sqlite"}"\n' + config_extra
    )


T0 = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def write_build(
    workdir: Path,
    uuid: UUID,
    actions: Mapping[str, str],
    events: Sequence[tuple[str, str | None, Any]],  # (type, action_id, data)
    *,
    build: int = 7,
    status: str = "running",
    flow: Path | None = None,
    start: datetime = T0,
    host: str = "",
    pid: int = 0,
) -> Path:
    """Record a build of `actions` ({action_id: step}) with `events` (type, action, data)."""
    record = BuildRecord(
        uuid=uuid,
        build=BuildId(build),
        domain="test",
        project="demo",
        flow=str(flow or workdir / "flow.yaml"),
        plan=hash_bytes(b"plan"),
        final_plan=None,
        status=status,  # type: ignore[arg-type]
        actions=dict(actions),
        created_at=start,
        host=host,
        pid=pid,
    )
    path = build_dir(workdir, uuid)
    record.write(path)
    lines = []
    for i, (type_, action_id, data) in enumerate(
        [("build_started", None, {"uuid": str(uuid)}), *events]
    ):
        event = Event(
            ts=start + timedelta(seconds=i),
            build=BuildId(build),
            type=type_,  # type: ignore[arg-type]
            action_id=action_id,
            data=dict(data),
        )
        lines.append(json.dumps(event.model_dump(mode="json"), sort_keys=True))
    (path / "events.jsonl").write_text("".join(line + "\n" for line in lines))
    return path


def append_event(path: Path, type_: str, action_id: str | None, data: Mapping[str, Any]) -> None:
    event = Event(
        ts=T0 + timedelta(hours=1),
        build=BuildId(7),
        type=type_,  # type: ignore[arg-type]
        action_id=action_id,
        data=dict(data),
    )
    with (path / "events.jsonl").open("a") as f:
        f.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")


def poison_statcache(db: Path, source: Path, cas_root: Path) -> None:
    """Record a wrong digest for `source` in the stat cache at `db` (a valid, non-racy entry).

    The wrong content is stored in the CAS, so a plan that trusts the stat cache succeeds with
    a different key: strict mode (`--rehash`, untrusted mounts) must not.
    """
    wrong = FsCAS(cas_root, DOMAIN).put_bytes(b"not the file's content\n")
    st = source.stat()
    with StatCache(db) as cache:
        cache.store(st, source, wrong, recorded_at=st.st_mtime + 60)
