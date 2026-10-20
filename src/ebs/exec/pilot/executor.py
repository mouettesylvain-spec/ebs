"""Pilot executor: a queue of actions and a pool of pilot jobs pulling it (P3-06, interfaces § 8).

For tiny actions (under 30 s) and very large regressions the per-job scheduling round costs more
than the work. `submit` puts the batch's actions in a queue of the metadata store
(`PilotQueueStore`, one pool per executor) and starts N pilot jobs as one job array,
N = min(queued actions, `[pilot].max_pilots`) minus the pilots of that shape still alive.
Each pilot (`worker`) leases an
action that fits its allocation, runs `ebs-runner` for it, reports and repeats; it exits when idle
for `idle_s` or when its walltime is nearly used.

A pilot's allocation (`queue.Allocation`) is sized by the step: the batch's largest cpus and
memory, the licenses it names (`-L` on the pilot job: licenses are held while a pilot idles, the
documented trade-off, hence the short default `idle_s`), the node features of its constraints and
of `[slurm].os_constraint`, and a walltime of `[pilot].walltime_s` (more if an action needs it).
Pilots of one shape serve every batch of that shape, so a retry reuses the live pilots.

`poll` asks the store for the state of every unfinished handle (one `queue_status` call):
`queued` is pending (with the pending reason of the shape's pilots: licenses when SLURM holds
them for licenses), `leased` running, `done` maps the runner's exit code like every executor
(`status_for_exit`), `lost` (the pilot stopped renewing its lease: it died) is
`infra_failed("node_fail")`, which the driver retries (R2). At most every `[slurm].poll_min_s`,
`poll` also refreshes the pilots with one `squeue` call and starts new ones where actions wait
and pilots ended; after `max_pilot_failures` pilots of a shape in a row failed (without one
ending normally), its waiting actions are reported `infra_failed("other")` instead of starting
pilots forever.

`cancel` withdraws the actions from the queue (one call); pilots running one of them see it at
their next lease renewal and stop the runner (SIGTERM, then SIGKILL). When no action of a shape
is left, its pilots are scancelled, which stops their runners at once. `close` scancels every
pilot still alive.

Pilot logs go to `<debug root>/<domain>/<build id|local>/slurm/pilot-%A_%a.out`, the runners'
output to `…/slurm/pilot/<pilot>/<item>.out`. Pilot job names are `ebs:<first 8 hex of the build
UUID|local>:pilot`. Nothing of this enters the action key.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID, uuid4

from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError, EbsError, ExecutorError, MetadataError
from ebs.core.log import get_logger
from ebs.exec.api import TERMINAL_JOB_STATES, JobHandle, JobStatus, SubmitBatch, status_for_exit
from ebs.exec.pilot.queue import Allocation, allocation_for, queue_item
from ebs.exec.pilot.worker import DEFAULT_KILL_GRACE_S, PilotConfig, pilot_arguments
from ebs.exec.slurm.accounts import BuildKind, JobPolicy
from ebs.exec.slurm.cli import ArraySpec, SbatchOptions, SlurmCli, write_env_file
from ebs.exec.slurm.executor import SlurmSettings
from ebs.exec.slurm.jobscript import render_jobscript
from ebs.exec.slurm.licenses import license_error, license_request
from ebs.exec.slurm.models import ArrayTaskId, JobRef, SlurmJobInfo
from ebs.exec.slurm.states import job_status
from ebs.meta.api import (
    QUEUE_LEASE_TTL_S,
    BuildId,
    BuildView,
    PendingReason,
    PilotQueueStore,
    QueueEntry,
)

__all__ = ["PilotExecutor", "PilotSettings"]

log = get_logger(__name__)

_PENDING: Final = JobStatus(state="pending")
_CANCELLED: Final = JobStatus(state="cancelled")
_LOST_AFTER_POLLS: Final = 10  # polls in a row SLURM does not know a pilot before it is gone

_PILOT_KEYS: Final = frozenset(
    {
        "max_pilots",
        "idle_s",
        "walltime_s",
        "lease_ttl_s",
        "poll_s",
        "kill_grace_s",
        "max_pilot_failures",
        "threshold",
        "short_s",
    }
)


def _positive(section: Mapping[str, object], name: str, default: float, kind: type) -> float:
    value = section.get(name, default)
    ok = isinstance(value, int) if kind is int else isinstance(value, int | float)
    if not ok or isinstance(value, bool) or value <= 0:  # type: ignore[operator]
        what = "a positive integer" if kind is int else "a positive number"
        raise ConfigError(f"[pilot].{name} must be {what}, got {value!r}")
    return float(value)  # type: ignore[arg-type]


@dataclass(frozen=True)
class PilotSettings:
    """The `[pilot]` section (P3-06)."""

    max_pilots: int = 50  # pilot jobs of one shape alive at once
    idle_s: float = 60.0  # a pilot with no work for this long exits (it holds its licenses)
    walltime_s: int = 4 * 3600  # SLURM time limit of a pilot job
    lease_ttl_s: int = QUEUE_LEASE_TTL_S  # visibility timeout; renewed every third of it
    poll_s: float = 1.0  # how often an idle pilot asks for work and checks its runner
    kill_grace_s: float = DEFAULT_KILL_GRACE_S  # SIGTERM -> SIGKILL of a withdrawn runner
    max_pilot_failures: int = 3  # pilots of a shape failing in a row before its actions fail
    threshold: int = 200  # execution: auto uses pilots for batches larger than this (R4)
    short_s: int = 30  # ... or for steps whose actions usually run shorter than this

    def __post_init__(self) -> None:
        for name in (
            "max_pilots", "idle_s", "walltime_s", "lease_ttl_s", "poll_s", "kill_grace_s",
            "max_pilot_failures", "threshold", "short_s",
        ):  # fmt: skip
            kind = float if name in ("idle_s", "poll_s", "kill_grace_s") else int
            _positive({name: getattr(self, name)}, name, 1, kind)

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, object]]) -> PilotSettings:
        section = config.get("pilot", {})
        unknown = sorted(set(section) - _PILOT_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [pilot]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(_PILOT_KEYS))}"
            )
        d = cls()
        return cls(
            max_pilots=int(_positive(section, "max_pilots", d.max_pilots, int)),
            idle_s=_positive(section, "idle_s", d.idle_s, float),
            walltime_s=int(_positive(section, "walltime_s", d.walltime_s, int)),
            lease_ttl_s=int(_positive(section, "lease_ttl_s", d.lease_ttl_s, int)),
            poll_s=_positive(section, "poll_s", d.poll_s, float),
            kill_grace_s=_positive(section, "kill_grace_s", d.kill_grace_s, float),
            max_pilot_failures=int(
                _positive(section, "max_pilot_failures", d.max_pilot_failures, int)
            ),
            threshold=int(_positive(section, "threshold", d.threshold, int)),
            short_s=int(_positive(section, "short_s", d.short_s, int)),
        )


class PilotStore(PilotQueueStore, Protocol):
    """What the pilot executor needs of the metadata store."""

    def get_build(self, build: BuildId) -> BuildView: ...


@dataclass(frozen=True)
class _Shape:
    """Pilots that may serve each other's actions: one allocation, one node exclusion list."""

    allocation: Allocation
    exclude: tuple[str, ...]


@dataclass(eq=False)
class _Item:
    handle: JobHandle
    item: int
    shape: _Shape
    build: BuildId | None
    status: JobStatus = _PENDING


@dataclass(eq=False)
class _Pilot:
    ref: JobRef
    shape: _Shape
    status: JobStatus = _PENDING
    unknown_polls: int = 0


@dataclass
class _Log:
    dir: Path
    env_file: Path


class PilotExecutor:
    """Runs actions through pilot jobs pulling from a queue in `store` (one pool, one domain)."""

    name = "pilot"

    def __init__(
        self,
        *,
        cli: SlurmCli,
        store: PilotStore,
        domain: str,
        settings: SlurmSettings,
        pilot: PilotSettings | None = None,
        pilot_argv: Sequence[str] = ("ebs-pilot",),
        runner_argv: Sequence[str] = ("ebs-runner",),
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        project: str | None = None,
        build_kind: BuildKind = "dev",
        pool: UUID | None = None,
    ) -> None:
        if not pilot_argv or not runner_argv:
            raise ExecutorError("pilot_argv and runner_argv must name the commands to start")
        self._cli = cli
        self._store = store
        self._domain = domain
        self._settings = settings
        self._pilot = pilot or PilotSettings()
        self._policy = JobPolicy.resolve(
            account_map=settings.account_map,
            qos=settings.qos,
            os_constraint=settings.os_constraint,
            project=project,
            build_kind=build_kind,
        )
        self._pilot_argv = list(pilot_argv)
        self._runner_argv = tuple(runner_argv)
        self._env = dict(os.environ if env is None else env)
        self._clock = clock or SystemClock()
        self.pool = pool or uuid4()
        self._items: dict[JobHandle, _Item] = {}
        self._pilots: list[_Pilot] = []
        self._failures: dict[_Shape, int] = {}
        self._failed: set[_Shape] = set()  # shapes whose waiting actions are given up
        self._last_upkeep: float | None = None
        self._uuids: dict[BuildId, UUID] = {}
        self._logs: dict[BuildId | None, _Log] = {}

    # --- Executor --------------------------------------------------------------------------------

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        if batch.domain != self._domain:
            raise ExecutorError(
                f"batch for domain {batch.domain!r} given to a pilot executor of domain "
                f"{self._domain!r}; open one executor per domain"
            )
        shape = _Shape(
            allocation_for(
                batch.actions,
                walltime_s=self._pilot.walltime_s,
                os_constraint=self._policy.os_constraint,
            ),
            batch.exclude_nodes,
        )
        items = [
            queue_item(a, plan=batch.plan, build=batch.build, exclude=batch.exclude_nodes)
            for a in batch.actions
        ]
        try:
            ids = self._store.queue_put(self._domain, self.pool, items)
        except MetadataError as exc:
            raise ExecutorError(f"cannot queue step {batch.step!r} for the pilots: {exc}") from exc
        queued = [
            _Item(JobHandle(self.name, str(i), a.action_id), i, shape, batch.build)
            for i, a in zip(ids, batch.actions, strict=True)
        ]
        for q in queued:
            self._items[q.handle] = q
        self._failed.discard(shape)  # a retry gets fresh pilots
        try:
            self._top_up(shape, batch.build)
        except EbsError as exc:
            self._withdraw(queued)
            raise ExecutorError(
                f"starting pilots for step {batch.step!r} failed: {exc}; its actions were "
                "withdrawn from the queue"
            ) from exc
        return [q.handle for q in queued]

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        items = [self._lookup(h) for h in handles]
        # Fresh item states first: topping up from stale ones starts pilots for actions that
        # already run. Then the pending reasons, from the pilots as the upkeep just saw them.
        self._refresh([i for i in items if i.status.state not in TERMINAL_JOB_STATES])
        self._upkeep()
        for i in items:
            if i.status.state == "pending":
                i.status = JobStatus(state="pending", pending_reason=self._pending_reason(i.shape))
        return {i.handle: i.status for i in items}

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        live = [self._lookup(h) for h in handles]
        live = [i for i in live if i.status.state not in TERMINAL_JOB_STATES]
        if not live:
            return
        try:
            self._refresh(live)  # an action that already finished keeps its state
        except ExecutorError as exc:  # cancelling matters more than exact final states
            log.warning("cannot read the pilot queue before cancelling", error=str(exc))
        live = [i for i in live if i.status.state not in TERMINAL_JOB_STATES]
        if not live:
            return
        self._withdraw(live)
        log.info("pilot actions cancelled", count=len(live))

    # --- extras ----------------------------------------------------------------------------------

    def close(self) -> None:
        """scancel every pilot still alive (their running actions report `cancelled`)."""
        self._scancel([p for p in self._pilots if p.status.state not in TERMINAL_JOB_STATES])

    def pilot_refs(self) -> list[JobRef]:
        """Every pilot job (array task) this executor started, in order."""
        return [p.ref for p in self._pilots]

    def query_pilots(self) -> dict[JobRef, SlurmJobInfo]:
        """What SLURM says about the pilots now (one query), for diagnostics and tests."""
        return self._cli.query(self.pilot_refs())

    # --- queue -----------------------------------------------------------------------------------

    def _lookup(self, handle: JobHandle) -> _Item:
        found = self._items.get(handle)
        if found is None:
            raise ExecutorError(
                f"unknown job handle {handle}: it was not submitted to this pilot executor"
            )
        return found

    def _refresh(self, items: Sequence[_Item]) -> None:
        if not items:
            return
        try:
            entries = self._store.queue_status(self._domain, self.pool, [i.item for i in items])
        except MetadataError as exc:
            raise ExecutorError(f"cannot read the pilot queue: {exc}") from exc
        for i, entry in zip(items, entries, strict=True):
            i.status = self._status(i, entry)

    def _status(self, item: _Item, entry: QueueEntry) -> JobStatus:
        node = entry.node
        if entry.state == "queued":
            if item.shape in self._failed:
                return JobStatus(state="infra_failed", infra_reason="other")
            return JobStatus(state="pending", pending_reason=self._pending_reason(item.shape))
        if entry.state == "leased":
            return JobStatus(state="running", node=node)
        if entry.state == "done":
            assert entry.exit_code is not None  # set with state done
            return status_for_exit(entry.exit_code, entry.infra_reason).model_copy(
                update={"node": node}
            )
        if entry.state == "lost":  # its pilot died mid-action (R2)
            return JobStatus(state="infra_failed", infra_reason="node_fail", node=node)
        return _CANCELLED

    def _withdraw(self, items: Sequence[_Item]) -> None:
        try:
            self._store.queue_cancel(self._domain, self.pool, [i.item for i in items])
        except MetadataError as exc:
            raise ExecutorError(f"cannot withdraw actions from the pilot queue: {exc}") from exc
        for i in items:
            i.status = _CANCELLED
        shapes = {i.shape for i in items}
        idle = [
            p
            for p in self._pilots
            if p.shape in shapes
            and p.status.state not in TERMINAL_JOB_STATES
            and not self._waiting(p.shape, running=True)
        ]
        self._scancel(idle)

    def _waiting(self, shape: _Shape, *, running: bool = False) -> int:
        """Unfinished actions of `shape` (only the pending ones unless `running`)."""
        wanted = {"pending", "running"} if running else {"pending"}
        return sum(1 for i in self._items.values() if i.shape == shape and i.status.state in wanted)

    def _pending_reason(self, shape: _Shape) -> PendingReason:
        reasons = {
            p.status.pending_reason
            for p in self._pilots
            if p.shape == shape and p.status.state == "pending"
        }
        for reason in ("licenses", "priority", "other"):
            if reason in reasons:
                return reason
        return "resources"  # waiting for pilots, or for one of the busy pilots to be free

    # --- pilots ----------------------------------------------------------------------------------

    def _upkeep(self) -> None:
        """Refresh the pilots (one squeue, at most every poll_min_s) and start the missing."""
        now = self._clock.monotonic()
        if self._last_upkeep is not None and now - self._last_upkeep < self._settings.poll_min_s:
            return
        self._last_upkeep = now
        alive = [p for p in self._pilots if p.status.state not in TERMINAL_JOB_STATES]
        if alive:
            try:
                infos = self._cli.query([p.ref for p in alive])
            except ExecutorError as exc:  # the queue still works: try again next time
                log.warning("cannot query the pilot jobs", error=str(exc))
                return
            for p in alive:
                info = infos.get(p.ref)
                if info is None:
                    p.unknown_polls += 1
                    if p.unknown_polls >= _LOST_AFTER_POLLS:
                        p.status = JobStatus(state="infra_failed", infra_reason="other")
                        self._pilot_ended(p)
                    continue
                p.unknown_polls = 0
                p.status = job_status(info)
                if p.status.state in TERMINAL_JOB_STATES:
                    self._pilot_ended(p)
        builds = {s: b for s, b in self._shape_builds().items() if s not in self._failed}
        for shape, build in builds.items():
            try:
                self._top_up(shape, build)
            except EbsError as exc:
                log.warning("cannot start more pilots", error=str(exc))

    def _pilot_ended(self, p: _Pilot) -> None:
        if p.status.state == "done":
            self._failures[p.shape] = 0
            return
        if p.status.state == "cancelled":
            return
        count = self._failures.get(p.shape, 0) + 1
        self._failures[p.shape] = count
        log.warning("pilot failed", pilot=str(p.ref), status=p.status.model_dump(), failures=count)
        if count >= self._pilot.max_pilot_failures and self._waiting(p.shape):
            log.error(
                "pilots keep failing; giving up the actions waiting for them",
                failures=count,
                waiting=self._waiting(p.shape),
            )
            self._failures[p.shape] = 0
            self._failed.add(p.shape)
            waiting = [
                i
                for i in self._items.values()
                if i.shape == p.shape and i.status.state == "pending"
            ]
            try:
                self._store.queue_cancel(self._domain, self.pool, [i.item for i in waiting])
            except MetadataError as exc:
                log.warning("cannot withdraw the waiting actions", error=str(exc))
            for i in waiting:
                i.status = JobStatus(state="infra_failed", infra_reason="other")

    def _shape_builds(self) -> dict[_Shape, BuildId | None]:
        """Shapes with actions waiting, and the build to name their pilots after."""
        out: dict[_Shape, BuildId | None] = {}
        for i in self._items.values():
            if i.status.state == "pending":
                out.setdefault(i.shape, i.build)
        return out

    def _top_up(self, shape: _Shape, build: BuildId | None) -> None:
        alive = sum(
            1
            for p in self._pilots
            if p.shape == shape and p.status.state not in TERMINAL_JOB_STATES
        )
        want = min(self._waiting(shape), self._pilot.max_pilots) - alive
        if want <= 0:
            return
        self._submit_pilots(shape, build, want)

    def _submit_pilots(self, shape: _Shape, build: BuildId | None, count: int) -> None:
        alloc = shape.allocation
        logs = self._log(build)
        config = PilotConfig(
            domain=self._domain,
            pool=self.pool,
            allocation=alloc,
            runner_argv=self._runner_argv,
            log_dir=logs.dir / "pilot",
            idle_s=self._pilot.idle_s,
            lease_ttl_s=self._pilot.lease_ttl_s,
            poll_s=self._pilot.poll_s,
            kill_grace_s=self._pilot.kill_grace_s,
        )
        script = self._write_script(
            logs.dir, render_jobscript(pilot_arguments(self._pilot_argv, config))
        )
        uuid = self._build_uuid(build)
        licenses = license_request(
            alloc.license_map(),
            server=self._settings.license_server,
            fuzzy=self._settings.license_fuzzy,
        )
        opts = SbatchOptions(
            job_name=f"ebs:{uuid.hex[:8] if uuid else 'local'}:pilot",
            partition=self._settings.default_partition,
            account=self._policy.account,
            qos=self._policy.qos,
            cpus_per_task=alloc.cpus,
            mem_mb=alloc.mem_mb,
            time_limit_s=alloc.walltime_s,
            licenses=licenses,
            constraint=alloc.constraint,
            exclude=shape.exclude,
            array=ArraySpec(indices=tuple(range(count)), throttle=count),
            output=logs.dir / "pilot-%A_%a.out",
            workdir=logs.dir,
            export_file=logs.env_file,
            comment=None if uuid is None else str(uuid),
        )
        try:
            job_id = self._cli.submit(script, opts)
        except ExecutorError as exc:
            surfaced = license_error(exc, licenses, step="pilot")
            if surfaced is None:
                raise
            raise surfaced from exc
        self._pilots += [_Pilot(ArrayTaskId(job_id, i), shape) for i in range(count)]
        log.info("pilots submitted", job_id=job_id, count=count, cpus=alloc.cpus)

    def _scancel(self, pilots: Sequence[_Pilot]) -> None:
        if not pilots:
            return
        try:
            self._cli.cancel([p.ref for p in pilots])
        except ExecutorError as exc:
            log.warning("cannot cancel idle pilots; they exit after idle_s", error=str(exc))
            return
        for p in pilots:
            p.status = _CANCELLED

    # --- files -----------------------------------------------------------------------------------

    def _build_uuid(self, build: BuildId | None) -> UUID | None:
        if build is None:
            return None
        uuid = self._uuids.get(build)
        if uuid is None:
            try:
                uuid = self._uuids[build] = self._store.get_build(build).uuid
            except MetadataError as exc:
                raise ExecutorError(f"cannot read build {build}: {exc}") from exc
        return uuid

    def _log(self, build: BuildId | None) -> _Log:
        found = self._logs.get(build)
        if found is not None:
            return found
        root = self._settings.domains.debug_root(self._domain, self._settings.debug_root)
        assert root is not None  # the default is a path
        path = root / self._domain / ("local" if build is None else str(build)) / "slurm"
        try:
            (path / "jobs").mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="pilot-", suffix=".env", dir=path / "jobs")
            os.close(fd)
            write_env_file(Path(name), self._env)
        except OSError as exc:
            raise ExecutorError(
                f"cannot prepare the pilot log directory {path}: {exc}; check [debug].root and "
                f"that you are in the {self._domain!r} group"
            ) from exc
        found = self._logs[build] = _Log(path, Path(name))
        return found

    def _write_script(self, log_dir: Path, text: str) -> Path:
        try:
            fd, name = tempfile.mkstemp(prefix="pilot-", suffix=".sh", dir=log_dir / "jobs")
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.chmod(name, 0o750)
        except OSError as exc:
            raise ExecutorError(f"cannot write the pilot job script in {log_dir}: {exc}") from exc
        return Path(name)
