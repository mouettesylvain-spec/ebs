"""`ebs build`: plan, run the driver, show progress, exit with the build's code (R2).

The driver emits every event to the MetadataStore; a thin proxy (`_Tap`) also hands each one to
the view, so the build UUID (in `build_started`) is printed before anything runs. Views: a live
per-step table on a terminal, one line per event otherwise, JSON lines with `--json`.
`.ebs/builds/<uuid>/build.json` is written at the start and rewritten at the end (§ 14).

P2-08: `--detach` starts the build, then submits `ebs build --resume-into <uuid>` as a 1-CPU
SLURM job (`[driver]` partition, QOS and walltime) that drives it, and returns at once; the
driver job's output goes to `.ebs/builds/<uuid>/driver.out`. `--resume <build>` drives a running
build whose driver died (`ebs.driver.resume`). SIGTERM, which `scancel` sends to a driver job,
cancels the build's jobs like Ctrl-C.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import typer
from rich.live import Live
from rich.table import Table

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli._records import BuildRecord, action_states, build_dir, find_build
from ebs.cli.plan import FLOW_OPTION, STEPS_ARGUMENT, PlanResult, make_plan
from ebs.core.errors import ConfigError, DriverBusyError, EbsError, ExitCode, MetadataError
from ebs.driver.build import BuildOutcome, run_build, start_build
from ebs.driver.resume import resume_build
from ebs.driver.retry import ReasonRetry, RetrySettings
from ebs.driver.scheduler import DriverConfig
from ebs.exec.slurm.accounts import JobPolicy
from ebs.exec.slurm.cli import SbatchOptions, write_env_file
from ebs.exec.slurm.jobscript import render_jobscript
from ebs.flow.model import parse_duration
from ebs.gc.access import put_registered
from ebs.meta.api import BuildStatus, CacheMode, Event, MetadataStore
from ebs.plan.planfile import encode, plan_digest
from ebs.plan.types import Plan

__all__ = ["DriverJobSettings", "build_command"]

_STATUS_STYLE: Final = {"passed": "green", "failed": "red", "infra_failed": "red"}
_DRIVER_KEYS: Final = frozenset({"partition", "qos", "time"})
DEFAULT_DRIVER_TIME_S: Final = 3 * 86_400
_ENV_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class _Tap:
    """MetadataStore proxy: forwards every call; also shows each event once it is stored."""

    def __init__(self, store: MetadataStore, on_event: Callable[[Event], None]) -> None:
        self._store = store
        self._on_event = on_event

    def emit(self, event: Event) -> None:
        self._store.emit(event)
        self._on_event(event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


def _event_line(event: Event) -> str:
    parts: list[str] = [event.type]
    if event.action_id is not None:
        parts.append(event.action_id)
    parts.extend(f"{k}={v}" for k, v in sorted(event.data.items()) if k != "counts")
    if "counts" in event.data:
        counts = cast(dict[str, int], event.data["counts"])
        parts.extend(f"{k}={v}" for k, v in counts.items())
    return " ".join(parts)


def _summary(counts: Mapping[str, int]) -> str:
    return ", ".join(f"{state}={n}" for state, n in sorted(counts.items()))


class _View:
    """Shows the events of one build in the output's mode.

    `driver_here`: this process drives the build, so the record names its host and pid (which
    `ebs status` uses to notice a dead driver); a detaching process does not.
    """

    def __init__(
        self,
        out: Output,
        plan: Plan | None,
        services: SiteServices,
        flow: Path,
        *,
        driver_here: bool = True,
    ) -> None:
        self.out = out
        self.plan = plan
        self.services = services
        self.flow = flow
        self.driver_here = driver_here
        self.record: BuildRecord | None = None
        self.events: list[Event] = []
        self.live: Live | None = None

    def on_event(self, event: Event) -> None:
        self.events.append(event)
        if event.type == "build_started":
            self._started(event)
        if self.out.json:
            self.out.emit(event.model_dump(mode="json"))
        elif self.live is not None:
            self.live.update(self.table(), refresh=True)
        elif event.type != "build_started":
            self.out.line(_event_line(event))

    def _started(self, event: Event) -> None:
        assert self.plan is not None  # a resumed build starts no new record
        self.record = BuildRecord(
            uuid=event.data["uuid"],  # type: ignore[arg-type]
            build=event.build,
            domain=self.plan.domain,
            project=self.plan.project,
            flow=str(self.flow),
            plan=plan_digest(self.plan),
            final_plan=None,
            status="running",
            actions={a.action_id: a.step for a in self.plan.actions},
            created_at=event.ts,
            host=socket.gethostname() if self.driver_here else "",
            pid=os.getpid() if self.driver_here else 0,
        )
        self.record.write(build_dir(self.services.cwd, self.record.uuid))
        if not self.out.json:
            self.out.line(f"build {self.record.uuid} (id {self.record.build})")

    def resumed(self, record: BuildRecord) -> None:
        """Follow a recorded build that this process now drives."""
        self.record = record.model_copy(update={"host": socket.gethostname(), "pid": os.getpid()})
        self.record.write(build_dir(self.services.cwd, record.uuid))
        self.events = record.events(self.services.cwd)
        if not self.out.json:
            self.out.line(f"build {record.uuid} (id {record.build}): resuming")

    def table(self) -> Table:
        assert self.record is not None
        states = action_states(self.record.actions, self.events)
        per_step: dict[str, Counter[str]] = {}
        for action_id, state in states.items():
            per_step.setdefault(self.record.actions[action_id], Counter())[state] += 1
        table = Table(box=None, pad_edge=False, header_style="bold")
        table.add_column("STEP")
        table.add_column("STATES")
        for step, counts in per_step.items():
            table.add_row(step, _summary(counts))
        return table

    @contextmanager
    def running(self) -> Iterator[None]:
        if self.out.json or not self.out.tty:
            yield
            return
        with Live(console=self.out.out, auto_refresh=False, transient=False) as live:
            self.live = live
            try:
                yield
            finally:
                self.live = None

    def finish(self, status: BuildStatus, final_plan: Plan | None, cas_put: Any) -> None:
        if self.record is None:
            return
        digest = cas_put(encode(final_plan)) if final_plan is not None else None
        self.record = self.record.model_copy(update={"status": status, "final_plan": digest})
        self.record.write(build_dir(self.services.cwd, self.record.uuid))


@dataclass(frozen=True)
class DriverJobSettings:
    """The `[driver]` section: the SLURM job of a detached driver (P2-08 R1)."""

    partition: str | None = None  # None: [slurm].default_partition
    qos: str | None = None  # None: the QOS of the build's kind ([slurm.qos])
    time_s: int = DEFAULT_DRIVER_TIME_S  # walltime: the longest build it may drive

    @classmethod
    def from_config(cls, config: Mapping[str, Mapping[str, object]]) -> DriverJobSettings:
        section = config.get("driver", {})
        unknown = sorted(set(section) - _DRIVER_KEYS)
        if unknown:
            raise ConfigError(
                f"unknown key(s) in [driver]: {', '.join(unknown)}; "
                f"expected {', '.join(sorted(_DRIVER_KEYS))}"
            )
        names: dict[str, str | None] = {}
        for key in ("partition", "qos"):
            value = section.get(key, "")
            if not isinstance(value, str):
                raise ConfigError(f"[driver].{key} must be a string, got {value!r}")
            names[key] = value or None
        time = section.get("time")
        time_s = DEFAULT_DRIVER_TIME_S
        if time is not None:
            try:
                time_s = parse_duration(time) if isinstance(time, str) else -1
            except ValueError as exc:
                raise ConfigError(f"[driver].time: {exc}") from None
            if time_s < 0:
                raise ConfigError(f"[driver].time must be a duration such as 72h, got {time!r}")
        return cls(partition=names["partition"], qos=names["qos"], time_s=time_s)


class CacheChoice(StrEnum):  # an Enum: Typer turns a bad value into a usage error (exit 2)
    off = "off"
    read = "read"
    write = "write"


class ExecutorChoice(StrEnum):
    local = "local"
    slurm = "slurm"


JSON_LINES_OPTION = typer.Option(
    False, "--json", help="Print each event, then the result, as one JSON document per line."
)
CACHE_OPTION = typer.Option(
    None, "--cache", help="Action cache use: off, read (default) or write (CI and releases)."
)
EXECUTOR_OPTION = typer.Option(ExecutorChoice.local, "--executor", help="Where actions run.")


def _cache_mode(cache: CacheChoice | None, no_cache: bool) -> CacheMode:
    if no_cache and cache not in (None, CacheChoice.off):
        raise UsageError(f"--no-cache conflicts with --cache {cache.value}")
    return "off" if no_cache else (cache or CacheChoice.read).value


@handle_errors
def build_command(
    ctx: typer.Context,
    steps: list[str] = STEPS_ARGUMENT,
    flow_file: Path = FLOW_OPTION,
    keep_going: bool = typer.Option(
        False, "-k", "--keep-going", help="Keep running independent actions after a failure."
    ),
    cache: CacheChoice | None = CACHE_OPTION,
    no_cache: bool = typer.Option(False, "--no-cache", help="Same as --cache off."),
    rerun_failed: bool = typer.Option(
        False, "--rerun-failed", help="Rerun actions whose cached result is a test failure."
    ),
    executor: ExecutorChoice = EXECUTOR_OPTION,
    rehash: bool = typer.Option(False, "--rehash", help="Hash every source file."),
    json_mode: bool = JSON_LINES_OPTION,
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Hand the driver to a SLURM job and return: the build survives logout.",
    ),
    resume: str | None = typer.Option(
        None,
        "--resume",
        metavar="BUILD",
        help="Drive a running build whose driver died (UUID, prefix or id).",
    ),
    resume_into: str | None = typer.Option(
        None, "--resume-into", hidden=True, help="Drive a detached build (the driver job)."
    ),
) -> None:
    """Run the flow: cached actions are reused, the rest run on the executor."""
    services: SiteServices = ctx.obj
    mode = _cache_mode(cache, no_cache)
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = services.store(required=True)
    if store is None:  # pragma: no cover - required=True raises instead
        raise ConfigError("no metadata store")
    retry = ReasonRetry(RetrySettings.from_config(services.config()))  # bad [retry]: fail early
    config = DriverConfig(cache_mode=mode, rerun_failed=rerun_failed, keep_going=keep_going)
    target = resume if resume is not None else resume_into
    if target is not None:
        if resume is not None and resume_into is not None:
            raise UsageError("give --resume or --resume-into, not both")
        if detach:
            raise UsageError(
                "--detach starts a new build; to drive a running one in a job, run "
                "`ebs build --resume BUILD` in an sbatch script"
            )
        if steps or cache is not None or no_cache or rehash:
            raise UsageError(
                "--resume continues a build with its own plan and cache mode: it takes no "
                "steps, --cache, --no-cache or --rehash"
            )
        outcome = _resume(services, out, store, target, executor, config, retry)
    else:
        if detach and executor is not ExecutorChoice.slurm:
            raise UsageError(
                "--detach runs the driver in a 1-CPU SLURM job, where local actions would "
                "crowd it: add `--executor slurm`"
            )
        planned = make_plan(services, out, flow_file, targets=steps or (), rehash=rehash)
        if detach:
            _detach(services, out, store, planned, executor, config)
            return
        outcome = _run(services, out, store, planned, executor, config, retry)
    _report(out, outcome, json_mode)


def _run(
    services: SiteServices,
    out: Output,
    store: MetadataStore,
    planned: PlanResult,
    executor: ExecutorChoice,
    config: DriverConfig,
    retry: ReasonRetry,
) -> BuildOutcome:
    view = _View(out, planned.plan, services, planned.path)
    tapped = cast(MetadataStore, _Tap(store, view.on_event))
    log_dir = services.cwd / ".ebs" / "logs"
    plan = planned.plan
    try:
        with (
            services.open_executor(
                executor.value, domain=plan.domain, log_dir=log_dir, project=plan.project
            ) as ex,
            view.running(),
        ):
            outcome = run_build(
                plan,
                refiner=planned.planner,
                cas=planned.cas,
                store=tapped,
                executor=ex,
                workdir=services.cwd,
                user=services.user(),
                config=config,
                clock=services.clock,
                retry=retry,
                handle_sigint=True,
                handle_sigterm=True,
            )
    except DriverBusyError:
        raise  # another driver took the build over: the record is its to finish
    except BaseException as exc:
        view.finish(
            "cancelled" if isinstance(exc, KeyboardInterrupt) else "infra_failed", None, None
        )
        raise
    view.finish(
        outcome.status,
        outcome.plan,
        lambda data: put_registered(planned.cas, tapped, outcome.plan.domain, data),
    )
    return outcome


def _resume(
    services: SiteServices,
    out: Output,
    store: MetadataStore,
    target: str,
    executor: ExecutorChoice,
    config: DriverConfig,
    retry: ReasonRetry,
) -> BuildOutcome:
    record = find_build(services.cwd, target)
    view = _View(out, None, services, Path(record.flow))
    tapped = cast(MetadataStore, _Tap(store, view.on_event))
    cas = services.cas(record.domain)
    log_dir = services.cwd / ".ebs" / "logs"
    with services.open_executor(
        executor.value, domain=record.domain, log_dir=log_dir, project=record.project
    ) as ex:
        view.resumed(record)
        try:
            with view.running():
                outcome = resume_build(
                    record.build,
                    cas=cas,
                    store=tapped,
                    executor=ex,
                    workdir=services.cwd,
                    config=config,
                    clock=services.clock,
                    retry=retry,
                    handle_sigint=True,
                    handle_sigterm=True,
                )
        except DriverBusyError:
            raise  # another driver has the build, or it is over: its record stays as it is
        except BaseException as exc:
            view.finish(
                "cancelled" if isinstance(exc, KeyboardInterrupt) else "infra_failed", None, None
            )
            raise
    view.finish(
        outcome.status,
        outcome.plan,
        lambda data: put_registered(cas, tapped, outcome.plan.domain, data),
    )
    return outcome


def _detach(
    services: SiteServices,
    out: Output,
    store: MetadataStore,
    planned: PlanResult,
    executor: ExecutorChoice,
    config: DriverConfig,
) -> None:
    """Start the build here, drive it in a SLURM job (R1)."""
    driver = DriverJobSettings.from_config(services.config())  # bad [driver]: fail before
    slurm = services.slurm_settings()  # anything is recorded
    plan = planned.plan
    policy = JobPolicy.resolve(
        account_map=slurm.account_map,
        qos=slurm.qos,
        os_constraint=None,
        project=plan.project,
        build_kind="dev",
    )
    view = _View(out, plan, services, planned.path, driver_here=False)
    tapped = cast(MetadataStore, _Tap(store, view.on_event))
    started = start_build(
        plan,
        cas=planned.cas,
        store=tapped,
        workdir=services.cwd,
        user=services.user(),
        cache_mode=config.cache_mode,
        clock=services.clock,
    )
    argv = [
        *services.ebs_argv(),
        "build",
        "--resume-into",
        str(started.uuid),
        "--executor",
        executor.value,
    ]
    if config.keep_going:
        argv.append("--keep-going")
    if config.rerun_failed:
        argv.append("--rerun-failed")
    directory = build_dir(services.cwd, started.uuid)
    try:
        script = directory / "driver.sh"
        script.write_text(render_jobscript(argv), encoding="utf-8")
        script.chmod(0o750)
        env_file = directory / "driver.env"  # mode 0600: the user's environment
        write_env_file(
            env_file,
            {
                k: v
                for k, v in services.environ.items()
                if _ENV_NAME.match(k) and not k.startswith("SLURM_")
            },
        )
        opts = SbatchOptions(
            job_name=f"ebs:{started.uuid.hex[:8]}:driver",
            partition=driver.partition or slurm.default_partition,
            account=policy.account,
            qos=driver.qos or policy.qos,
            cpus_per_task=1,
            time_limit_s=driver.time_s,
            output=directory / "driver.out",
            workdir=services.cwd,
            export_file=env_file,
            comment=str(started.uuid),
        )
        job = services.slurm_cli().submit(script, opts)
    except (EbsError, OSError, ValueError) as exc:
        # If even that fails, the build stays `running` without a driver: `--resume` takes it.
        with contextlib.suppress(MetadataError):
            store.finish_build(started.build, "infra_failed")
        view.finish("infra_failed", None, None)
        if isinstance(exc, EbsError):
            raise
        raise ConfigError(f"cannot submit the driver job of build {started.uuid}: {exc}") from exc
    if out.json:
        out.emit(
            {
                "build": int(started.build),
                "uuid": str(started.uuid),
                "status": "running",
                "driver_job": str(job),
            }
        )
    else:
        out.line(
            f"driver job {job} submitted: follow the build with `ebs status {started.uuid}`, "
            f"cancel it with `scancel {job}`"
        )


def _report(out: Output, outcome: BuildOutcome, json_mode: bool) -> None:
    counts = Counter[str](outcome.states.values())
    if json_mode:
        out.emit(
            {
                "build": int(outcome.build),
                "uuid": str(outcome.uuid),
                "status": outcome.status,
                "exit_code": int(outcome.exit_code),
                "counts": dict(sorted(counts.items())),
            }
        )
    else:
        style = _STATUS_STYLE.get(outcome.status, "yellow")
        out.out.print(
            f"build {outcome.uuid}: [{style}]build {outcome.status}[/{style}] ({_summary(counts)})"
        )
    if outcome.exit_code != ExitCode.OK:
        raise typer.Exit(int(outcome.exit_code))
