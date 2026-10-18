"""SLURM executor: actions as sbatch jobs and job arrays (P1-02, interfaces.md § 8).

`submit` turns a batch into sbatch calls (`arrays.plan_jobs`: one plain job per single action,
one job array per batch, split at `max_array_size`), each running a fixed job script that execs
`ebs-runner` (`jobscript`). Every job id is recorded in the MetadataStore
(`actions.slurm_job_id`, state `pending`) before `submit` returns, so a restarted driver can
re-attach (P2-08: `adopt`). If a later sbatch call or a record fails, the jobs already submitted
for the batch are cancelled and `submit` raises ExecutorError: no job is ever left untracked.

`poll` makes one `SlurmCli.query` (one squeue, plus one sacct for jobs squeue forgot) for every
unfinished handle it is given and reports array tasks one by one; finished jobs are answered
from memory. `cancel` sends one scancel for all of them; jobs that already finished keep their
state, the others report `cancelled` from then on.

Back-pressure (R8, defaults from D5): at most `max_submits_per_min` sbatch calls in any 60 s
window, and no new batch while it would take the jobs in flight (array tasks counted one by one)
above `max_pending_jobs`. Over a limit, `submit` raises `ExecutorBusy` and starts nothing; the
driver keeps the batch queued. A batch is never refused when nothing is in flight (or no sbatch
call was made in the window), so one larger than a limit still runs, alone.

Job logs (runner stdout+stderr, including its infra line) go to
`<debug root>/<domain>/<build id|local>/slurm/%A_%a.out` (arrays) or `%j.out` (plain jobs);
the job scripts and env files sit next to them in `jobs/`. Job names are
`ebs:<first 8 hex of the build UUID|local>:<step>`, the comment field holds the build UUID.

Each sbatch call sets the partition, cpus, memory and time limit (the action's `resources.time`
plus `JOB_TIME_GRACE_S`, so the runner's own timeout fires first and it can still clean up and
report), and (P1-03) the licenses (`licenses`), the account and QOS of the build's project and
kind, and the node constraint (`accounts`). None of them is part of the action key.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final
from uuid import UUID

from ebs.cas.api import CAS
from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError, EbsError, ExecutorError
from ebs.core.log import get_logger
from ebs.exec.api import (
    TERMINAL_JOB_STATES,
    ExecutorBusy,
    JobHandle,
    JobStatus,
    SubmitBatch,
)
from ebs.exec.slurm.accounts import (
    BuildKind,
    JobPolicy,
    QosSettings,
    job_constraint,
    parse_account_map,
    parse_os_constraint,
    parse_qos,
)
from ebs.exec.slurm.arrays import JobPlan, plan_jobs, store_array_map
from ebs.exec.slurm.cli import ArraySpec, SbatchOptions, SlurmCli, write_env_file
from ebs.exec.slurm.jobscript import render_jobscript, runner_arguments
from ebs.exec.slurm.licenses import (
    DEFAULT_LICENSE_SERVER,
    batch_licenses,
    check_license_server,
    license_error,
    license_request,
)
from ebs.exec.slurm.models import ArrayTaskId, JobId, JobRef, SlurmJobInfo
from ebs.exec.slurm.states import job_status
from ebs.meta.api import BuildId, MetadataStore
from ebs.plan.types import ActionSpec
from ebs.runner.errors import EXIT_INFRA, parse_infra_reason

__all__ = ["JOB_TIME_GRACE_S", "LOST_AFTER_POLLS", "SlurmExecutor", "SlurmSettings"]

log = get_logger(__name__)

JOB_TIME_GRACE_S: Final = 120
"""Added to `resources.time` for SLURM's limit: runner timeout + kill grace + upload."""

_WINDOW_S: Final = 60.0
LOST_AFTER_POLLS: Final = 10
"""Polls in a row that SLURM does not know a job before it counts as lost (like the driver)."""
_LOG_TAIL: Final = 64 * 1024  # the runner's infra line is among its last words
_MIB: Final = 1 << 20
_DEFAULT_ENV: Final = ("PATH", "EBS_CONFIG")  # what the job needs to find ebs-runner + config
_PENDING: Final = JobStatus(state="pending")
_CANCELLED: Final = JobStatus(state="cancelled")

_SLURM_KEYS: Final = frozenset(
    {
        "default_partition",
        "max_array_size",
        "array_throttle",
        "max_pending_jobs",
        "max_submits_per_min",
        "poll_min_s",
        "poll_max_s",
        "bin_dir",
        "license_server",
        "license_fuzzy",
        "account_map",
        "qos",
        "os_constraint",
    }
)


# --- settings --------------------------------------------------------------------------------


def _int_key(section: Mapping[str, object], name: str, default: int) -> int:
    value = section.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ConfigError(f"[slurm].{name} must be a positive integer, got {value!r}")
    return value


def _seconds_key(section: Mapping[str, object], name: str, default: float) -> float:
    value = section.get(name, default)
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"[slurm].{name} must be a positive number of seconds, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class SlurmSettings:
    """The `[slurm]` section (defaults: decision D5) plus `[debug].root`."""

    debug_root: Path = Path("/debug")
    default_partition: str | None = None  # None: the cluster's default partition
    max_array_size: int = 1000
    array_throttle: int = 200
    max_pending_jobs: int = 2000
    max_submits_per_min: int = 60
    poll_min_s: float = 5.0  # also how long a batch refused for too many jobs should wait
    poll_max_s: float = 60.0
    bin_dir: Path | None = None  # where sbatch & co live; None: on PATH
    license_server: str = DEFAULT_LICENSE_SERVER  # remote licenses are `<feature>@<server>`
    license_fuzzy: bool = False  # request `<feature>` only (RemoteFuzzyMatch)
    account_map: Mapping[str, str] = field(default_factory=dict)  # project -> account
    qos: QosSettings = field(default_factory=QosSettings)
    os_constraint: str | None = None  # node feature of the OS image the toolchains need

    def __post_init__(self) -> None:
        for name in ("max_array_size", "array_throttle", "max_pending_jobs", "max_submits_per_min"):
            _int_key({name: getattr(self, name)}, name, 1)
        if not 0 < self.poll_min_s <= self.poll_max_s:
            raise ConfigError(
                f"[slurm].poll_max_s ({self.poll_max_s}) must be at least poll_min_s "
                f"({self.poll_min_s}), and both positive"
            )

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, object]]) -> SlurmSettings:
        """Validate `[slurm]` and `[debug].root`; ConfigError names the bad key."""
        section = config.get("slurm", {})
        unknown = sorted(set(section) - _SLURM_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [slurm]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(_SLURM_KEYS))}"
            )
        partition = section.get("default_partition", "")
        if not isinstance(partition, str):
            raise ConfigError(f"[slurm].default_partition must be a string, got {partition!r}")
        bin_dir = section.get("bin_dir")
        if bin_dir is not None and (
            not isinstance(bin_dir, str) or not Path(bin_dir).is_absolute()
        ):
            raise ConfigError(f"[slurm].bin_dir must be an absolute path, got {bin_dir!r}")
        fuzzy = section.get("license_fuzzy", False)
        if not isinstance(fuzzy, bool):
            raise ConfigError(f"[slurm].license_fuzzy must be true or false, got {fuzzy!r}")
        debug_root = config.get("debug", {}).get("root", "/debug")
        if not isinstance(debug_root, str) or not Path(debug_root).is_absolute():
            raise ConfigError(f"[debug].root must be an absolute path, got {debug_root!r}")
        return cls(
            debug_root=Path(debug_root),
            default_partition=partition or None,
            max_array_size=_int_key(section, "max_array_size", 1000),
            array_throttle=_int_key(section, "array_throttle", 200),
            max_pending_jobs=_int_key(section, "max_pending_jobs", 2000),
            max_submits_per_min=_int_key(section, "max_submits_per_min", 60),
            poll_min_s=_seconds_key(section, "poll_min_s", 5.0),
            poll_max_s=_seconds_key(section, "poll_max_s", 60.0),
            bin_dir=Path(bin_dir) if bin_dir is not None else None,
            license_server=check_license_server(
                section.get("license_server", DEFAULT_LICENSE_SERVER)
            ),
            license_fuzzy=fuzzy,
            account_map=parse_account_map(section.get("account_map")),
            qos=parse_qos(section.get("qos")),
            os_constraint=parse_os_constraint(section.get("os_constraint")),
        )


# --- the executor ----------------------------------------------------------------------------


@dataclass(eq=False)
class _Job:
    handle: JobHandle
    ref: JobRef
    log: Path  # the job's output file (sbatch patterns expanded)
    status: JobStatus = _PENDING
    unknown_polls: int = 0  # polls in a row SLURM did not know it


@dataclass
class _Submitted:
    """What one `submit` call has started so far (undone if a later step fails)."""

    jobs: list[_Job] = field(default_factory=list)
    job_ids: list[JobId] = field(default_factory=list)


class SlurmExecutor:
    """Runs each action as `<runner_argv> --plan … (--action …|--array-map …) --domain …`.

    `project` (the plan's) picks the SLURM account from `[slurm.account_map]` and `build_kind`
    the QOS; both are resolved here, so a project without an account fails before any job.
    """

    name = "slurm"

    def __init__(
        self,
        *,
        cli: SlurmCli,
        cas: CAS,
        settings: SlurmSettings,
        store: MetadataStore | None = None,
        runner_argv: Sequence[str] = ("ebs-runner",),
        env: Mapping[str, str] | None = None,
        clock: Clock | None = None,
        project: str | None = None,
        build_kind: BuildKind = "dev",
    ) -> None:
        if not runner_argv:
            raise ExecutorError("runner_argv is empty: give the ebs-runner command to start")
        self._cli = cli
        self._cas = cas
        self._settings = settings
        self._policy = JobPolicy.resolve(
            account_map=settings.account_map,
            qos=settings.qos,
            os_constraint=settings.os_constraint,
            project=project,
            build_kind=build_kind,
        )
        self._store = store
        self._runner_argv = list(runner_argv)
        if env is None:
            env = {k: os.environ[k] for k in _DEFAULT_ENV if k in os.environ}
        self._env = dict(env)
        self._clock = clock or SystemClock()
        self._jobs: dict[JobHandle, _Job] = {}
        self._arrays: dict[JobId, list[_Job]] = {}
        self._submit_times: deque[float] = deque()
        self._uuids: dict[BuildId, UUID] = {}

    # --- Executor --------------------------------------------------------------------------------

    def submit(self, batch: SubmitBatch) -> list[JobHandle]:
        if batch.domain != self._cas.domain:
            raise ExecutorError(
                f"batch for domain {batch.domain!r} given to a SLURM executor whose CAS is for "
                f"domain {self._cas.domain!r}; open one executor per domain"
            )
        if batch.build is not None and self._store is None:
            raise ExecutorError(
                "the SLURM executor needs the metadata store to run actions of a build "
                "(it records each job id there before submit returns)"
            )
        plans = plan_jobs(
            batch.actions,
            max_array_size=self._settings.max_array_size,
            throttle=self._settings.array_throttle,
        )
        self._admit(len(batch.actions), len(plans))
        uuid = self._build_uuid(batch.build)
        log_dir = self._log_dir(batch)
        done = _Submitted()
        try:
            env_file = self._write_env(log_dir, batch.step)
            for job in plans:
                self._submit_one(batch, job, uuid, log_dir, env_file, done)
        except EbsError as exc:
            self._undo(done)
            raise ExecutorError(
                f"submitting step {batch.step!r} failed after {len(done.job_ids)} of "
                f"{len(plans)} sbatch call(s): {exc}"
                + ("; the jobs already submitted were cancelled" if done.job_ids else "")
            ) from exc
        for j in done.jobs:
            self._jobs[j.handle] = j
            if isinstance(j.ref, ArrayTaskId):
                self._arrays.setdefault(j.ref.array_job_id, []).append(j)
        return [j.handle for j in done.jobs]

    def poll(self, handles: Sequence[JobHandle]) -> dict[JobHandle, JobStatus]:
        jobs = [self._lookup(h) for h in handles]
        seen = self._refresh([j for j in jobs if j.status.state not in TERMINAL_JOB_STATES])
        # A job SLURM does not know (yet, or any more) is left out; the driver counts misses.
        return {
            j.handle: j.status
            for j in jobs
            if j.status.state in TERMINAL_JOB_STATES or j.handle in seen
        }

    def cancel(self, handles: Sequence[JobHandle]) -> None:
        """One scancel for all unfinished jobs; they report `cancelled` from now on.

        A runner may have posted its result just before; the driver tolerates a result for a
        job reported `cancelled` (as with the local executor).
        """
        live = [self._lookup(h) for h in handles]
        live = [j for j in live if j.status.state not in TERMINAL_JOB_STATES]
        if not live:
            return
        try:
            self._refresh(live)  # a job that already finished keeps its state
        except ExecutorError as exc:  # cancelling matters more than exact final states
            log.warning("cannot query SLURM before cancelling; cancelling all", error=str(exc))
        live = [j for j in live if j.status.state not in TERMINAL_JOB_STATES]
        if not live:
            return
        self._cli.cancel(self._cancel_refs(live))
        for j in live:
            j.status = _CANCELLED
        log.info("jobs cancelled", count=len(live))

    # --- extras ----------------------------------------------------------------------------------

    def adopt(self, build: BuildId, handles: Sequence[JobHandle]) -> None:
        """Track jobs another driver of `build` submitted (P2-08: a resumed driver re-attaches
        to the job ids recorded in `actions.slurm_job_id`); `poll` and `cancel` then treat
        them like jobs submitted here. Their state is unknown until the next poll."""
        log_dir = self._settings.debug_root / self._cas.domain / str(build) / "slurm"
        adopted: list[_Job] = []
        for handle in handles:
            if handle.executor != self.name:
                raise ExecutorError(
                    f"cannot adopt {handle}: it was handed out by executor {handle.executor!r}"
                )
            text = handle.job_id
            ref: JobRef = ArrayTaskId.parse(text) if "_" in text else JobId(text)
            if isinstance(ref, str) and not ref.isdigit():
                raise ExecutorError(f"cannot adopt {handle}: {text!r} is not a SLURM job id")
            adopted.append(_Job(handle, ref, log_dir / f"{text}.out"))
        for j in adopted:
            if j.handle in self._jobs:
                continue
            self._jobs[j.handle] = j
            if isinstance(j.ref, ArrayTaskId):
                self._arrays.setdefault(j.ref.array_job_id, []).append(j)

    def job_ref(self, handle: JobHandle) -> JobRef:
        """The SLURM job (or array task) behind `handle`."""
        return self._lookup(handle).ref

    def in_flight(self) -> int:
        """Jobs not known to be finished, array tasks counted one by one."""
        return sum(1 for j in self._jobs.values() if j.status.state not in TERMINAL_JOB_STATES)

    # --- submission ------------------------------------------------------------------------------

    def _admit(self, tasks: int, calls: int) -> None:
        now = self._clock.monotonic()
        while self._submit_times and self._submit_times[0] <= now - _WINDOW_S:
            self._submit_times.popleft()
        in_flight = self.in_flight()
        limit = self._settings.max_pending_jobs
        if in_flight and in_flight + tasks > limit:
            raise ExecutorBusy(
                f"{in_flight} SLURM jobs in flight; {tasks} more would exceed "
                f"[slurm].max_pending_jobs = {limit}",
                retry_after_s=self._settings.poll_min_s,
            )
        rate = self._settings.max_submits_per_min
        if self._submit_times and len(self._submit_times) + calls > rate:
            raise ExecutorBusy(
                f"{len(self._submit_times)} sbatch calls in the last minute; {calls} more would "
                f"exceed [slurm].max_submits_per_min = {rate}",
                retry_after_s=self._submit_times[0] + _WINDOW_S - now,
            )

    def _submit_one(
        self,
        batch: SubmitBatch,
        job: JobPlan,
        uuid: UUID | None,
        log_dir: Path,
        env_file: Path,
        done: _Submitted,
    ) -> None:
        ids = [a.action_id for a in job.actions]
        if job.is_array:
            array_map = store_array_map(self._cas, batch.plan, ids)
            argv = runner_arguments(
                self._runner_argv,
                plan=batch.plan,
                domain=batch.domain,
                build=batch.build,
                array_map=array_map,
            )
            output = log_dir / "%A_%a.out"
            array = ArraySpec(indices=tuple(range(len(ids))), throttle=job.throttle)
        else:
            argv = runner_arguments(
                self._runner_argv,
                plan=batch.plan,
                domain=batch.domain,
                build=batch.build,
                action=ids[0],
            )
            output = log_dir / "%j.out"
            array = None
        script = self._write_script(log_dir, batch.step, render_jobscript(argv))
        cpus, mem, time = _resources(job.actions)
        licenses = license_request(
            batch_licenses(job.actions),
            server=self._settings.license_server,
            fuzzy=self._settings.license_fuzzy,
        )
        opts = SbatchOptions(
            job_name=f"ebs:{uuid.hex[:8] if uuid else 'local'}:{batch.step}",
            partition=self._settings.default_partition,
            account=self._policy.account,
            qos=self._policy.qos,
            cpus_per_task=cpus,
            mem_mb=None if mem is None else max(1, math.ceil(mem / _MIB)),
            time_limit_s=None if time is None else time + JOB_TIME_GRACE_S,
            licenses=licenses,
            constraint=job_constraint(
                self._policy.os_constraint,
                sorted({c for a in job.actions if (c := a.resources.constraint) is not None}),
            ),
            exclude=batch.exclude_nodes,
            array=array,
            output=output,
            workdir=log_dir,
            export_file=env_file,
            comment=None if uuid is None else str(uuid),
        )
        try:
            job_id = self._cli.submit(script, opts)
        except ExecutorError as exc:
            surfaced = license_error(exc, licenses, step=batch.step)
            if surfaced is None:
                raise
            raise surfaced from exc
        self._submit_times.append(self._clock.monotonic())
        done.job_ids.append(job_id)
        if job.is_array:
            jobs = [
                _Job(
                    JobHandle(self.name, f"{job_id}_{i}", action_id),
                    ArrayTaskId(job_id, i),
                    log_dir / f"{job_id}_{i}.out",
                )
                for i, action_id in enumerate(ids)
            ]
        else:
            jobs = [_Job(JobHandle(self.name, job_id, ids[0]), job_id, log_dir / f"{job_id}.out")]
        done.jobs.extend(jobs)
        if batch.build is not None:
            assert self._store is not None  # checked in submit
            for j in jobs:  # R7: before submit returns, so a restarted driver can re-attach
                self._store.set_action_state(
                    batch.build, j.handle.action_id, "pending", slurm_job_id=j.handle.job_id
                )

    def _undo(self, done: _Submitted) -> None:
        if not done.job_ids:
            return
        try:
            self._cli.cancel(done.job_ids)
        except ExecutorError as exc:
            log.error(
                "could not cancel the jobs of a failed submission; cancel them by hand",
                job_ids=done.job_ids,
                error=str(exc),
            )

    def _build_uuid(self, build: BuildId | None) -> UUID | None:
        if build is None:
            return None
        uuid = self._uuids.get(build)
        if uuid is None:
            assert self._store is not None  # checked in submit
            uuid = self._uuids[build] = self._store.get_build(build).uuid
        return uuid

    def _log_dir(self, batch: SubmitBatch) -> Path:
        build = "local" if batch.build is None else str(batch.build)
        path = self._settings.debug_root / batch.domain / build / "slurm"
        try:
            (path / "jobs").mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ExecutorError(
                f"cannot create the SLURM log directory {path}: {exc}; check [debug].root and "
                f"that you are in the {batch.domain!r} group"
            ) from exc
        return path

    def _write_env(self, log_dir: Path, step: str) -> Path:
        try:
            fd, name = tempfile.mkstemp(prefix=f"{step}-", suffix=".env", dir=log_dir / "jobs")
            os.close(fd)
            write_env_file(Path(name), self._env)
        except OSError as exc:
            raise ExecutorError(
                f"cannot write the job env file in {log_dir / 'jobs'}: {exc}"
            ) from exc
        return Path(name)

    def _write_script(self, log_dir: Path, step: str, text: str) -> Path:
        try:
            fd, name = tempfile.mkstemp(prefix=f"{step}-", suffix=".sh", dir=log_dir / "jobs")
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.chmod(name, 0o750)
        except OSError as exc:
            raise ExecutorError(
                f"cannot write the job script in {log_dir / 'jobs'}: {exc}"
            ) from exc
        return Path(name)

    # --- polling ---------------------------------------------------------------------------------

    def _lookup(self, handle: JobHandle) -> _Job:
        job = self._jobs.get(handle)
        if job is None:
            raise ExecutorError(
                f"unknown job handle {handle}: it was not submitted to this SLURM executor"
            )
        return job

    def _refresh(self, jobs: Sequence[_Job]) -> set[JobHandle]:
        """Update `jobs` with ONE query; returns the handles SLURM knew about."""
        if not jobs:
            return set()
        infos = self._cli.query([j.ref for j in jobs])
        seen: set[JobHandle] = set()
        for j in jobs:
            info = infos.get(j.ref)
            if info is None:
                j.unknown_polls += 1
                if j.unknown_polls >= LOST_AFTER_POLLS:  # gone: stop counting it as in flight
                    log.warning("SLURM no longer knows the job", job_id=j.handle.job_id)
                    j.status = JobStatus(state="infra_failed", infra_reason="other")
                continue
            j.unknown_polls = 0
            seen.add(j.handle)
            j.status = self._status(j, info)
        return seen

    def _status(self, job: _Job, info: SlurmJobInfo) -> JobStatus:
        reason = None
        if (
            info.state in ("COMPLETED", "FAILED")
            and info.exit_code == EXIT_INFRA
            and not info.signal
        ):
            reason = parse_infra_reason(_tail(job.log))
        return job_status(info, infra_reason=reason)

    def _cancel_refs(self, jobs: Sequence[_Job]) -> list[JobRef]:
        """Whole arrays by their id when every unfinished task of them is cancelled."""
        chosen = {j.handle for j in jobs}
        refs: list[JobRef] = []
        whole: set[JobId] = set()
        for j in jobs:
            if not isinstance(j.ref, ArrayTaskId):
                refs.append(j.ref)
                continue
            array = j.ref.array_job_id
            if array in whole:
                continue
            tasks = self._arrays[array]
            if all(t.handle in chosen or t.status.state in TERMINAL_JOB_STATES for t in tasks):
                whole.add(array)
                refs.append(array)
            else:
                refs.append(j.ref)
        return refs


def _resources(actions: Sequence[ActionSpec]) -> tuple[int | None, int | None, int | None]:
    """cpus, memory (bytes) and time (s) for one sbatch call: the largest request of `actions`."""

    def largest(attr: str) -> int | None:
        values: list[int] = []
        for a in actions:
            value = getattr(a.resources, attr)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool):
                raise ExecutorError(
                    f"action {a.action_id}: resources.{attr} is {value!r}, not a number; the "
                    "planner must expand it before submission"
                )
            values.append(value)
        return max(values) if values else None

    return largest("cpus"), largest("mem"), largest("time")


def _tail(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _LOG_TAIL))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
