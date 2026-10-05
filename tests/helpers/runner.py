"""Shared helpers for runner tests: hand-built action specs, plans in a tmp CAS, a harness."""

from __future__ import annotations

import io
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.flow.model import Resources
from ebs.meta.api import ActionRow, BuildCreate, BuildId, CacheMode, Event, ResultManifest
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.keys import with_key
from ebs.plan.planfile import encode
from ebs.plan.types import (
    ActionSpec,
    DebugSpec,
    FlowInfo,
    InputRef,
    OutputSpec,
    Plan,
    PlanToolchain,
    RuleRef,
    SourceInput,
    ToolchainRef,
)
from ebs.rules.registry import RuleRegistry
from ebs.rules.shell import BASH_ARGV, SCRIPT_PATH, ShellRule
from ebs.runner.config import RunnerSettings
from ebs.runner.main import Runner, RunRequest

DOMAIN = "test"
HOST = "node-test"


def shell_spec(
    script: str,
    *,
    action_id: str = "s",
    inputs: Sequence[InputRef] = (),
    outputs: Sequence[OutputSpec] = (),
    env: Mapping[str, str] | None = None,
    runtime_env: Mapping[str, str] | None = None,
    config_files: Mapping[str, str] | None = None,
    toolchain: ToolchainRef | None = None,
    time: int | None = None,
    cpus: int | None = None,
    argv: Sequence[str] | None = None,
) -> ActionSpec:
    """A `shell` action running `script` (bash), or `argv` if given, with its key computed."""
    spec = ActionSpec(
        action_id=action_id,
        step=action_id.split("[", 1)[0],
        rule=RuleRef(ShellRule.kind, ShellRule.version),
        argv=tuple(argv) if argv is not None else (*BASH_ARGV, SCRIPT_PATH),
        params={},
        env=dict(env or {}),
        toolchain=toolchain,
        inputs=tuple(sorted(inputs, key=lambda r: r.logical_path)),
        outputs=tuple(sorted(outputs, key=lambda o: o.name)),
        config_files={**({SCRIPT_PATH: script} if argv is None else {}), **(config_files or {})},
        resources=Resources(cpus=cpus, time=None if time is None else f"{time}s"),
        licenses={},
        debug=DebugSpec(),
        domain=DOMAIN,
        key=None,
        runtime_env=dict(runtime_env or {}),
    )
    return with_key(spec)


def source_file(cas: FsCAS, logical_path: str, data: bytes) -> InputRef:
    """A source file input whose bytes are in the CAS."""
    return InputRef(logical_path, "file", SourceInput("srcs", "**"), cas.put_bytes(data))


def source_tree(cas: FsCAS, logical_path: str, root: Path) -> InputRef:
    """A tree input uploaded from `root`, with a content id (like a deterministic output)."""
    return InputRef(logical_path, "tree", SourceInput("libs", "**"), cas.put_tree(root))


def store_plan(
    cas: FsCAS, *specs: ActionSpec, toolchains: Mapping[str, PlanToolchain] | None = None
) -> Digest:
    plan = Plan(
        ebs_version="test",
        domain=DOMAIN,
        project="demo",
        flow=FlowInfo(),
        toolchains=dict(toolchains or {}),
        actions=specs,
        edges=(),
    )
    return cas.put_bytes(encode(plan))


def settings(tmp_path: Path, **overrides: object) -> RunnerSettings:
    base: dict[str, object] = {
        "cas_root": tmp_path / "cas",
        "scratch_dir": tmp_path / "scratch",
        "kill_grace_s": 0.5,
    }
    base.update(overrides)
    return RunnerSettings(**base)  # type: ignore[arg-type]


def new_build(store: InMemoryMetadataStore, *action_ids: str, cache_mode: CacheMode) -> BuildId:
    build = store.create_build(
        BuildCreate(
            uuid=uuid4(),
            domain=DOMAIN,
            project="demo",
            plan_digest=hash_bytes(b"plan"),
            flow_repo="",
            flow_commit="",
            flow_dirty=False,
            user_name="tester",
            cache_mode=cache_mode,
        )
    )
    store.add_actions(build, [ActionRow(action_id=a, step=a.split("[", 1)[0]) for a in action_ids])
    return build


class RecordingStore(InMemoryMetadataStore):
    """The in-memory store, also keeping every posted result, cache put and event in order."""

    def __init__(self) -> None:
        super().__init__(clock=FakeClock())
        self.results: dict[tuple[int, str], ResultManifest] = {}
        self.cache_puts: list[tuple[str, Digest]] = []
        self.events: list[Event] = []

    def record_result(self, build: BuildId, action_id: str, result: ResultManifest) -> None:
        super().record_result(build, action_id, result)
        self.results[(build, action_id)] = result

    def cache_put(self, domain: str, key: Digest, result: ResultManifest) -> bool:
        self.cache_puts.append((domain, key))
        return super().cache_put(domain, key, result)

    def emit(self, event: Event) -> None:
        super().emit(event)
        self.events.append(event)


@dataclass
class Harness:
    """A runner wired to a tmp CAS, an in-memory store and the built-in rules."""

    tmp_path: Path
    cas: FsCAS
    store: RecordingStore = field(default_factory=RecordingStore)
    caller_env: dict[str, str] = field(default_factory=dict)
    settings_overrides: dict[str, object] = field(default_factory=dict)
    out: io.StringIO = field(default_factory=io.StringIO)
    err: io.StringIO = field(default_factory=io.StringIO)

    def runner(self) -> Runner:
        return Runner(
            cas=self.cas,
            store=self.store,
            rules=RuleRegistry([ShellRule()]),
            settings=settings(self.tmp_path, **self.settings_overrides),
            caller_env=self.caller_env,
            host=HOST,
            clock=FakeClock(),
            out=self.out,
            err=self.err,
        )

    def run(
        self,
        *specs: ActionSpec,
        action_id: str | None = None,
        cache_mode: CacheMode = "write",
        toolchains: Mapping[str, PlanToolchain] | None = None,
        keep_scratch: bool = False,
    ) -> tuple[int, BuildId]:
        plan = store_plan(self.cas, *specs, toolchains=toolchains)
        build = new_build(self.store, *(s.action_id for s in specs), cache_mode=cache_mode)
        wanted = action_id if action_id is not None else specs[0].action_id
        code = self.runner().run(RunRequest(plan, wanted, build, keep_scratch=keep_scratch))
        return code, build

    def result(self, build: BuildId, action_id: str = "s") -> ResultManifest | None:
        """The manifest the runner posted for `action_id`, if any."""
        return self.store.results.get((build, action_id))

    def log_text(self, manifest: ResultManifest) -> str:
        assert manifest.log is not None
        with self.cas.open(manifest.log) as f:
            return f.read().decode()

    def scratch_entries(self) -> list[str]:
        root = self.tmp_path / "scratch"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*")) if root.exists() else []


def all_paths(root: Path) -> set[str]:
    """Every file, dir and link below `root`, relative, POSIX."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            found.add(str((Path(dirpath) / name).relative_to(root)))
    return found
