"""`ebs status [BUILD]`: per-step action counts by state, from the local build record (R3).

States come from the driver's events (`.ebs/builds/<uuid>/events.jsonl`), so status works
without the metadata store; pending actions are split by reason (`pending: licenses`,
`pending: resources`, …). `--watch` redraws until the build finishes.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Final

import typer
from rich.table import Table

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli._records import BuildRecord, action_states, build_status, find_build
from ebs.cli.plan import JSON_OPTION
from ebs.driver.events import Event

__all__ = ["status_command"]

STATE_ORDER: Final = (
    "queued", "submitted", "pending: licenses", "pending: resources", "pending: priority",
    "pending: other", "running", "infra_failed", "retrying", "cached", "done", "failed",
    "skipped", "cancelled",
)  # fmt: skip


def _order(state: str) -> tuple[int, str]:
    return (STATE_ORDER.index(state) if state in STATE_ORDER else len(STATE_ORDER), state)


def _sorted(counts: Counter[str]) -> dict[str, int]:
    return {s: counts[s] for s in sorted(counts, key=_order)}


def status_document(record: BuildRecord, events: list[Event]) -> dict[str, Any]:
    states = action_states(record.actions, events)
    per_step: dict[str, Counter[str]] = {}
    for action_id, state in states.items():
        per_step.setdefault(record.actions.get(action_id, "?"), Counter())[state] += 1
    totals = Counter(states.values())
    return {
        "v": 1,
        "uuid": str(record.uuid),
        "build": int(record.build),
        "status": build_status(record, events),
        "flow": record.flow,
        "created_at": record.created_at.isoformat(),
        "steps": [
            {"step": step, "actions": sum(c.values()), "states": _sorted(c)}
            for step, c in per_step.items()
        ],
        "totals": _sorted(totals),
    }


def _print(out: Output, doc: dict[str, Any]) -> None:
    total = sum(doc["totals"].values())
    out.line(f"build {doc['uuid']} (id {doc['build']}): {doc['status']}, {total} actions")
    columns = list(doc["totals"])
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("STEP")
    table.add_column("ACTIONS", justify="right")
    for state in columns:
        table.add_column(state, justify="right")  # lowercase: "pending: licenses"
    for step in doc["steps"]:
        table.add_row(
            step["step"], str(step["actions"]), *(str(step["states"].get(s, 0)) for s in columns)
        )
    out.out.print(table)


@handle_errors
def status_command(
    ctx: typer.Context,
    build: str | None = typer.Argument(
        None, metavar="[BUILD]", help="Build UUID (or prefix) or id; default: the latest here."
    ),
    watch: bool = typer.Option(False, "--watch", "-w", help="Refresh until the build ends."),
    interval: float = typer.Option(2.0, "--interval", min=0.1, help="Seconds between refreshes."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show a build's actions per step and state (pending split by licenses/resources)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    record = find_build(services.cwd, build)
    while True:
        doc = status_document(record, record.events(services.cwd))
        if json_mode:
            out.emit(doc)
        else:
            _print(out, doc)
        if not watch or doc["status"] != "running":
            return
        services.clock.sleep(interval)
