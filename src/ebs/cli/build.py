"""`ebs build`: plan, run the driver, show progress, exit with the build's code (R2).

The driver emits every event to the MetadataStore; a thin proxy (`_Tap`) also hands each one to
the view, so the build UUID (in `build_started`) is printed before anything runs. Views: a live
per-step table on a terminal, one line per event otherwise, JSON lines with `--json`.
`.ebs/builds/<uuid>/build.json` is written at the start and rewritten at the end (§ 14).
"""

from __future__ import annotations

import os
import socket
from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, cast

import typer
from rich.live import Live
from rich.table import Table

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli._records import BuildRecord, action_states, build_dir
from ebs.cli.plan import FLOW_OPTION, STEPS_ARGUMENT, make_plan
from ebs.core.errors import ConfigError, ExitCode
from ebs.driver.build import BuildOutcome, run_build
from ebs.driver.scheduler import DriverConfig
from ebs.meta.api import BuildStatus, CacheMode, Event, MetadataStore
from ebs.plan.planfile import encode, plan_digest
from ebs.plan.types import Plan

__all__ = ["build_command"]

_STATUS_STYLE: Final = {"passed": "green", "failed": "red", "infra_failed": "red"}


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
    """Shows the events of one build in the output's mode."""

    def __init__(self, out: Output, plan: Plan, services: SiteServices, flow: Path) -> None:
        self.out = out
        self.plan = plan
        self.services = services
        self.flow = flow
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
            host=socket.gethostname(),
            pid=os.getpid(),
        )
        self.record.write(build_dir(self.services.cwd, self.record.uuid))
        if not self.out.json:
            self.out.line(f"build {self.record.uuid} (id {self.record.build})")

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


class CacheChoice(StrEnum):  # an Enum: Typer turns a bad value into a usage error (exit 2)
    off = "off"
    read = "read"
    write = "write"


class ExecutorChoice(StrEnum):
    local = "local"


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
) -> None:
    """Run the flow: cached actions are reused, the rest run on the executor."""
    services: SiteServices = ctx.obj
    mode = _cache_mode(cache, no_cache)
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = services.store(required=True)
    if store is None:  # pragma: no cover - required=True raises instead
        raise ConfigError("no metadata store")
    planned = make_plan(services, out, flow_file, targets=steps or (), rehash=rehash)
    view = _View(out, planned.plan, services, planned.path)
    tapped = cast(MetadataStore, _Tap(store, view.on_event))
    log_dir = services.cwd / ".ebs" / "logs"
    outcome: BuildOutcome | None = None
    try:
        with (
            services.open_executor(
                executor.value, domain=planned.plan.domain, log_dir=log_dir
            ) as ex,
            view.running(),
        ):
            outcome = run_build(
                planned.plan,
                refiner=planned.planner,
                cas=planned.cas,
                store=tapped,
                executor=ex,
                workdir=services.cwd,
                user=services.user(),
                config=DriverConfig(
                    cache_mode=mode, rerun_failed=rerun_failed, keep_going=keep_going
                ),
                clock=services.clock,
                handle_sigint=True,
            )
    except BaseException as exc:
        view.finish(
            "cancelled" if isinstance(exc, KeyboardInterrupt) else "infra_failed", None, None
        )
        raise
    view.finish(outcome.status, outcome.plan, planned.cas.put_bytes)
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
