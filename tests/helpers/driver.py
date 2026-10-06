"""Driver test helpers: hand-built DAG plans and a ScriptedExecutor that plays the runner's part.

`ScriptedExecutor` follows the executor contract (interfaces.md § 8) and the runner's duties
(§ 9): it loads the submitted plan from the CAS, checks that the action's key and input ids are
set and that every producer has finished, then, when a job ends, posts a ResultManifest
(`record_result`, plus `cache_put` in cache mode `write`). Outcomes are scripted per attempt.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.core.clock import Clock, FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.driver.build import BuildOutcome, run_build
from ebs.driver.retry import RetryPolicy
from ebs.driver.scheduler import DriverConfig
from ebs.exec.api import JobHandle, JobStatus, SubmitBatch
from ebs.flow.model import Resources
from ebs.meta.api import (
    BuildId,
    InfraReason,
    MetadataStore,
    OutputResult,
    ResourceUsage,
    ResultManifest,
    RunnerInfo,
)
from ebs.meta.memory import InMemoryMetadataStore
from ebs.plan.keys import nondeterministic_output_id
from ebs.plan.planfile import decode
from ebs.plan.planner import Planner
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    FlowInfo,
    InputRef,
    OutputSpec,
    Plan,
    RuleRef,
    SourceInput,
)
from tests.helpers.plan import planner as make_planner

DOMAIN = "test"

Outcome: TypeAlias = (
    "Literal['pass', 'fail', 'hang', 'lost', 'vanished'] | tuple[Literal['infra'], InfraReason]"
)
"""pass/fail: the tool ran; hang: runs until cancelled; lost: `done` but no result posted;
vanished: the job reports `cancelled` although nobody asked; ("infra", reason): exit 75."""


@dataclass(frozen=True)
class Node:
    """One action of a hand-built DAG: what it consumes and what it produces."""

    deps: tuple[tuple[str, str], ...] = ()  # (producer action_id, output name)
    outputs: Mapping[str, bool] = field(default_factory=lambda: {"out": True})  # name -> det.
    step: str | None = None  # default: the action id up to "["
    source: str = ""  # changes the source input's id, hence the key
    optional: frozenset[str] = frozenset()  # outputs declared optional
    resources: Resources = field(default_factory=Resources)
    licenses: Mapping[str, int] = field(default_factory=dict)


def make_plan(
    cas: CAS,
    nodes: Mapping[str, Node],
    *,
    flow: FlowInfo | None = None,
) -> Plan:
    """A plan of `nodes` (in topological order), keyed as far as known, via `Planner.refine`."""
    specs: list[ActionSpec] = []
    edges: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for action_id, node in nodes.items():
        inputs = [
            InputRef(
                f"src/{action_id}",
                "file",
                SourceInput("src", f"src/{action_id}"),
                hash_bytes(f"{action_id}:{node.source}".encode()),
            )
        ]
        for producer, output in node.deps:
            assert producer in seen, f"{producer} must come before {action_id}"
            inputs.append(
                InputRef(f"{output}/{producer}", "file", ActionOutputInput(producer, output), None)
            )
            edges.append((producer, output, action_id))
        seen.add(action_id)
        specs.append(
            ActionSpec(
                action_id=action_id,
                step=node.step or action_id.split("[", 1)[0],
                rule=RuleRef("shell", "1"),
                argv=("true",),
                params={},
                env={},
                toolchain=None,
                inputs=tuple(sorted(inputs, key=lambda r: r.logical_path)),
                outputs=tuple(
                    OutputSpec(name, f"out/{name}", "file", det, name in node.optional)
                    for name, det in sorted(node.outputs.items())
                ),
                config_files={},
                resources=node.resources,
                licenses=dict(node.licenses),
                debug=DebugSpec(),
                domain=DOMAIN,
                key=None,
            )
        )
    plan = Plan(
        ebs_version="0.1.0",
        domain=DOMAIN,
        project="demo",
        flow=flow or FlowInfo(),
        toolchains={},
        actions=tuple(specs),
        edges=tuple(sorted(edges)),
    )
    return refiner(cas).refine(plan, {})


def refiner(cas: CAS) -> Planner:
    planner, _ = make_planner(cas, {})
    return planner


@dataclass
class Script:
    outcomes: list[Outcome]  # one per attempt; the last one repeats
    pending_polls: int = 0
    running_polls: int = 1
    outputs: dict[str, bytes] = field(default_factory=dict)  # content per output name
    omit: frozenset[str] = frozenset()  # outputs the "tool" does not produce
    ignores_cancel: bool = False  # the job finishes anyway (it was already ending)


@dataclass
class _Job:
    handle: JobHandle
    spec: ActionSpec
    build: BuildId | None
    outcome: Outcome
    script: Script
    polls: int = 0
    status: JobStatus = field(
        default_factory=lambda: JobStatus(state="pending", pending_reason="resources")
    )


class ScriptedExecutor:
    """An in-process Executor whose jobs follow a per-action script (default: pass)."""

    name = "scripted"

    def __init__(
        self,
        store: MetadataStore,
        cas: CAS,
        *,
        default: Outcome = "pass",
        on_poll: Callable[[int], None] | None = None,
    ) -> None:
        self._store = store
        self._cas = cas
        self._default = default
        self._on_poll = on_poll
        self._scripts: dict[str, Script] = {}
        self._jobs: dict[JobHandle, _Job] = {}
        self._attempts: dict[str, int] = {}
        self.batches: list[SubmitBatch] = []
        self.submitted: list[str] = []  # action ids, in submission order (one per attempt)
        self.submitted_specs: list[ActionSpec] = []  # as loaded from the plan in the CAS
        self.finished: list[str] = []  # action ids whose job reached `done`
        self.poll_sizes: list[int] = []
        self.cancelled: list[str] = []

    def script(
        self,
        action_id: str,
        *outcomes: Outcome,
        pending_polls: int = 0,
        running_polls: int = 1,
        outputs: Mapping[str, bytes] | None = None,
        omit: Iterable[str] = (),
        ignores_cancel: bool = False,
    ) -> None:
        self._scripts[action_id] = Script(
            list(outcomes) or [self._default],
            pending_polls,
            running_polls,
            dict(outputs or {}),
            frozenset(omit),
            ignores_cancel,
        )

    @property
    def polls(self) -> int:
        return len(self.poll_sizes)

    def runs(self, action_id: str) -> int:
        return self.submitted.count(action_id)

    # --- Executor protocol ----------------------------------------------------------------------

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        self.batches.append(batch)
        plan = decode(_read(self._cas, batch.plan))
        handles = []
        for given in batch.actions:
            spec = plan.action(given.action_id)  # what the runner would load
            assert spec == given, f"{spec.action_id}: the plan in the CAS differs from the batch"
            assert spec.key is not None, f"{spec.action_id} submitted without a key"
            assert all(ref.id is not None for ref in spec.inputs), spec.action_id
            for ref in spec.inputs:
                if isinstance(ref.source, ActionOutputInput) and batch.build is not None:
                    produced = self._store.get_result(batch.build, ref.source.action_id)
                    early = f"{spec.action_id} submitted before {ref.source.action_id} succeeded"
                    assert produced is not None, early
                    assert produced.status == "passed", early
            attempt = self._attempts.get(spec.action_id, 0)
            self._attempts[spec.action_id] = attempt + 1
            script = self._scripts.get(spec.action_id) or Script([self._default])
            outcome = script.outcomes[min(attempt, len(script.outcomes) - 1)]
            handle = JobHandle(self.name, f"{spec.action_id}#{attempt + 1}", spec.action_id)
            self._jobs[handle] = _Job(handle, spec, batch.build, outcome, script)
            self.submitted.append(spec.action_id)
            self.submitted_specs.append(spec)
            handles.append(handle)
        return handles

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        self.poll_sizes.append(len(handles))
        if self._on_poll is not None:
            self._on_poll(len(self.poll_sizes))
        return {h: self._advance(self._jobs[h]) for h in handles}

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        for h in handles:
            job = self._jobs[h]
            if job.status.state not in {"done", "infra_failed", "cancelled"}:
                self.cancelled.append(h.action_id)
                if job.script.ignores_cancel:  # it was finishing: the next poll says done
                    job.outcome = "pass"
                    job.polls = job.script.pending_polls + job.script.running_polls
                else:
                    job.status = JobStatus(state="cancelled")

    # --- job progress ---------------------------------------------------------------------------

    def _advance(self, job: _Job) -> JobStatus:
        if job.status.state in {"done", "infra_failed", "cancelled"}:
            return job.status
        job.polls += 1
        script = job.script
        if job.polls <= script.pending_polls:
            job.status = JobStatus(state="pending", pending_reason="licenses")
        elif job.outcome == "hang" or job.polls <= script.pending_polls + script.running_polls:
            job.status = JobStatus(state="running")
        elif isinstance(job.outcome, tuple):
            job.status = JobStatus(state="infra_failed", infra_reason=job.outcome[1], exit_code=75)
        elif job.outcome == "vanished":
            job.status = JobStatus(state="cancelled")
        elif job.outcome == "lost":
            job.status = JobStatus(state="done", exit_code=0)  # but nothing was posted
        else:
            self._post(job, passed=job.outcome == "pass")
            job.status = JobStatus(state="done", exit_code=0)
            self.finished.append(job.spec.action_id)
        return job.status

    def _post(self, job: _Job, *, passed: bool) -> None:
        spec = job.spec
        assert spec.key is not None
        result = result_for(spec, passed=passed, contents=job.script.outputs, omit=job.script.omit)
        if job.build is None:
            return
        self._store.record_result(job.build, spec.action_id, result)
        if self._store.get_build(job.build).cache_mode == "write":
            self._store.cache_put(spec.domain, spec.key, result)


def result_for(
    spec: ActionSpec,
    *,
    passed: bool = True,
    contents: Mapping[str, bytes] | None = None,
    omit: Iterable[str] = (),
) -> ResultManifest:
    """The manifest a runner would post for `spec` (default output bytes: `<action>/<name>`)."""
    assert spec.key is not None
    contents = contents or {}
    outputs = {}
    for out in spec.outputs:
        if out.name in omit:
            continue
        data = contents.get(out.name, f"{spec.action_id}/{out.name}".encode())
        digest = hash_bytes(data)
        oid = digest if out.deterministic else nondeterministic_output_id(spec.key, out.name)
        outputs[out.name] = OutputResult(digest=digest, id=oid, type="file", size=len(data))
    return ResultManifest(
        action_key=spec.key,
        status="passed" if passed else "failed",
        exit_code=0 if passed else 1,
        inputs={ref.logical_path: ref.id for ref in spec.inputs if ref.id is not None},
        outputs=outputs,
        log=None,
        summary={},
        resources=ResourceUsage(max_rss_kb=0, cpu_s=0, wall_s=0),
        runner=RunnerInfo(version="0.1.0", host="node-a"),
    )


def _read(cas: CAS, digest: Digest) -> bytes:
    with cas.open(digest) as f:
        return f.read()


class SleepRecorder(FakeClock):
    """A FakeClock that also records every sleep."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        super().sleep(seconds)


def with_resources(node: Node, **kwargs: object) -> Node:
    return dataclasses.replace(node, resources=Resources(**kwargs))  # type: ignore[arg-type]


@dataclass
class Env:
    cas: FsCAS
    store: InMemoryMetadataStore
    clock: SleepRecorder
    workdir: Path

    def plan(self, nodes: Mapping[str, Node], **kwargs: Any) -> Plan:
        return make_plan(self.cas, nodes, **kwargs)

    def executor(self, **kwargs: Any) -> ScriptedExecutor:
        return ScriptedExecutor(self.store, self.cas, **kwargs)

    def run(
        self,
        plan: Plan,
        executor: ScriptedExecutor,
        *,
        retry: RetryPolicy | None = None,
        handle_sigint: bool = False,
        ci_job: str | None = None,
        clock: Clock | None = None,
        **config: Any,
    ) -> BuildOutcome:
        defaults: dict[str, Any] = {"poll_min_s": 1.0, "poll_max_s": 4.0}
        return run_build(
            plan,
            refiner=refiner(self.cas),
            cas=self.cas,
            store=self.store,
            executor=executor,
            workdir=self.workdir,
            user="alice",
            ci_job=ci_job,
            config=DriverConfig(**{**defaults, **config}),
            clock=clock or self.clock,
            retry=retry,
            handle_sigint=handle_sigint,
        )


def make_env(tmp_path: Path) -> Env:
    clock = SleepRecorder()
    workdir = tmp_path / "work"
    workdir.mkdir()
    return Env(
        cas=FsCAS(tmp_path / "cas", DOMAIN, clock=clock),
        store=InMemoryMetadataStore(clock=clock),
        clock=clock,
        workdir=workdir,
    )
