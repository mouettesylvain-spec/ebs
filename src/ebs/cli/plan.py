"""`ebs plan`: expand the flow, predict cache hits, explain why each miss reruns (R1).

Prediction follows the driver: look up every keyed action in the action cache; the output ids of
passed hits refine the plan (`Planner.refine`), which may key more actions; repeat until nothing
changes. Misses are explained by `diff_plans` against the last build of the same flow in this
directory (`.ebs/builds`), or `--diff BUILD`. The `--json` document: interfaces.md § 14.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import typer
from rich.table import Table

from ebs.cas.fs import FsCAS
from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli._records import BuildRecord, find_build
from ebs.core.digest import Digest
from ebs.core.errors import CasError, ConfigError, FlowError, PlanError
from ebs.flow.loader import load_flow
from ebs.flow.model import Flow
from ebs.meta.api import MetadataStore, ResultManifest
from ebs.plan.diff import diff_plans
from ebs.plan.planfile import decode, plan_digest
from ebs.plan.planner import Planner
from ebs.plan.types import ActionDiff, ActionOutputInput, FlowInfo, GitInfo, Plan
from ebs.sources.gitids import run_git

__all__ = ["PlanResult", "make_plan", "plan_command", "predict"]

MAX_REASONS: Final = 50
"""Rerun reasons printed as text; `--json` has all of them."""

Cache = Literal["hit", "miss", "unknown"]

FLOW_OPTION = typer.Option(
    Path("flow.yaml"), "-f", "--flow", help="The flow file.", show_default=True
)
STEPS_ARGUMENT = typer.Argument(None, metavar="[STEP]...", help="Only these steps.")
JSON_OPTION = typer.Option(False, "--json", help="Print a JSON document instead of tables.")


@dataclass
class PlanResult:
    path: Path  # absolute flow file
    flow: Flow
    planner: Planner
    cas: FsCAS
    plan: Plan
    warnings: list[str] = field(default_factory=list)


def _flow_path(services: SiteServices, flow_file: Path) -> Path:
    path = Path(os.path.normpath(services.cwd / flow_file))
    if not path.is_file():
        raise FlowError(
            f"no flow file {path}; run ebs in the flow's directory or pass -f PATH",
            file=str(flow_file),
        )
    return path


_GIT_FAILURES: Final = (OSError, subprocess.CalledProcessError, UnicodeDecodeError)


def flow_info(path: Path, git: Callable[[Sequence[str], Path], bytes] = run_git) -> FlowInfo:
    """Where the flow lives in git (recorded in plan.json; never hashed into keys)."""
    try:
        top = Path(git(["git", "rev-parse", "--show-toplevel"], path.parent).decode().strip())
        commit = git(["git", "rev-parse", "HEAD"], path.parent).decode().strip()
        dirty = bool(git(["git", "status", "--porcelain", "--untracked-files=no"], top).strip())
    except _GIT_FAILURES:  # not a repository, no commit yet, or no git
        return FlowInfo(path=path.name)
    try:
        repo = git(["git", "config", "--get", "remote.origin.url"], top).decode().strip()
    except _GIT_FAILURES:  # no remote
        repo = ""
    try:
        rel = path.resolve().relative_to(top.resolve()).as_posix()
    except ValueError:  # pragma: no cover - the flow is inside the repository it was found in
        rel = path.name
    return FlowInfo(path=rel, git=GitInfo(repo=repo, commit=commit, dirty=dirty))


def make_plan(
    services: SiteServices,
    out: Output,
    flow_file: Path,
    *,
    targets: Sequence[str] = (),
    rehash: bool = False,
) -> PlanResult:
    """Load the flow and expand it into a plan (sources snapshotted into the CAS)."""
    path = _flow_path(services, flow_file)
    flow = load_flow(path)
    cas = services.cas(flow.domain)
    warnings: list[str] = []

    def warn(message: str) -> None:
        warnings.append(message)
        out.warn(message)

    with services.open_statcache(warn) as statcache:
        sources = services.snapshotter(cas, statcache, rehash=rehash)
        planner = Planner(cas, sources, services.toolchains(path.parent), services.rules())
        plan = planner.plan(
            flow, base=path.parent, info=flow_info(path), targets=targets, rehash=rehash
        )
        for mismatch in sources.audit_mismatches:
            warn(
                f"stat cache audit: {mismatch} changed without a visible stat change; it was "
                "rehashed. If this repeats, add its mount to [stat_cache].untrusted_mounts"
            )
    return PlanResult(path, flow, planner, cas, plan, warnings)


@dataclass(frozen=True)
class Prediction:
    plan: Plan  # refined through the cache hits
    hits: dict[str, ResultManifest]  # action_id -> cached result
    looked_up: frozenset[str]  # keyed actions (hit or miss)


def predict(plan: Plan, store: MetadataStore, planner: Planner) -> Prediction:
    hits: dict[str, ResultManifest] = {}
    looked_up: set[str] = set()
    while True:
        produced: dict[tuple[str, str], Digest] = {}
        for spec in plan.actions:
            if spec.key is None or spec.action_id in looked_up:
                continue
            looked_up.add(spec.action_id)
            result = store.cache_get(plan.domain, spec.key)
            if result is None:
                continue
            hits[spec.action_id] = result
            if result.status == "passed":
                declared = {o.name for o in spec.outputs}
                produced.update(
                    ((spec.action_id, name), out.id)
                    for name, out in result.outputs.items()
                    if name in declared
                )
        if not produced:
            return Prediction(plan, hits, frozenset(looked_up))
        plan = planner.refine(plan, produced)


def _load_plan(cas: FsCAS, digest: Digest) -> Plan:
    with cas.open(digest) as f:
        return decode(f.read())


def _baseline(
    services: SiteServices, out: Output, result: PlanResult, ref: str | None
) -> tuple[BuildRecord | None, Plan | None]:
    try:
        record = find_build(services.cwd, ref, flow=None if ref else result.path)
    except ConfigError:
        if ref is not None:
            raise
        return None, None
    digest = record.final_plan or record.plan
    try:
        cas = result.cas if record.domain == result.flow.domain else services.cas(record.domain)
        return record, _load_plan(cas, digest)
    except (CasError, PlanError) as exc:
        if ref is not None:
            raise
        message = f"cannot compare with build {record.uuid}: {exc}"
        result.warnings.append(message)
        out.warn(message)
        return None, None


def _document(
    result: PlanResult,
    prediction: Prediction | None,
    record: BuildRecord | None,
    diffs: dict[str, ActionDiff],
    removed: list[str],
) -> dict[str, Any]:
    plan = prediction.plan if prediction else result.plan
    actions: list[dict[str, Any]] = []
    per_step: dict[str, Counter[str]] = {}
    for spec in plan.actions:
        hit = prediction.hits.get(spec.action_id) if prediction else None
        cache: Cache
        if prediction is None or spec.action_id not in prediction.looked_up:
            cache = "unknown"
        else:
            cache = "hit" if hit is not None else "miss"
        diff = diffs.get(spec.action_id)
        pending = [
            f"depends on {ref.source.action_id}"
            for ref in spec.inputs
            if ref.id is None and isinstance(ref.source, ActionOutputInput)
        ]
        actions.append(
            {
                "action_id": spec.action_id,
                "step": spec.step,
                "key": None if spec.key is None else str(spec.key),
                "cache": cache,
                "cached_status": hit.status if hit else None,
                "change": {
                    "status": diff.status if diff else None,
                    "fields": list(diff.changed) if diff else [],
                    "pending": sorted(set(pending)),
                },
            }
        )
        counts = per_step.setdefault(spec.step, Counter())
        counts["actions"] += 1
        counts[cache] += 1
    totals = Counter[str]()
    for counts in per_step.values():
        totals.update(counts)
    keys = ("actions", "hit", "miss", "unknown")
    return {
        "v": 1,
        "flow": result.plan.flow.path,
        "domain": plan.domain,
        "project": plan.project,
        "plan": str(plan_digest(result.plan)),
        "cache": "available" if prediction else "unavailable",
        "baseline": {"build": str(record.uuid) if record else None},
        "totals": {k: totals[k] for k in keys},
        "steps": [{"step": s, **{k: c[k] for k in keys}} for s, c in per_step.items()],
        "actions": actions,
        "removed": removed,
        "warnings": result.warnings,
    }


def _reason(action: dict[str, Any], baseline: str | None) -> str | None:
    change = action["change"]
    if action["cache"] == "hit":
        return None
    if action["cache"] == "unknown" and change["pending"]:
        why = ", ".join(change["pending"])
        return f"unknown until it runs ({why})" + (
            f"; changed: {', '.join(change['fields'])}" if change["fields"] else ""
        )
    if baseline is None:
        return None
    match change["status"]:
        case "changed" | "unknown":
            return ", ".join(change["fields"]) or "key schema"
        case "added":
            return f"new action (not in build {baseline})"
        case "unchanged":
            return f"same key as build {baseline}, but not in the cache"
    return None


def _print(out: Output, doc: dict[str, Any]) -> None:
    t = doc["totals"]
    base = doc["baseline"]["build"]
    vs = f"vs build {base}" if base else "no previous build"
    out.line(
        f"plan {doc['plan'][:19]} of {doc['flow']}: {t['actions']} actions, {t['hit']} hit, "
        f"{t['miss']} miss, {t['unknown']} unknown ({vs})"
    )
    if doc["cache"] == "unavailable":
        out.line("cache: no metadata store configured ([metadata].url), hits are unknown")
    table = Table(box=None, pad_edge=False, header_style="bold")
    for name in ("STEP", "ACTIONS", "HIT", "MISS", "UNKNOWN"):
        table.add_column(name, justify="left" if name == "STEP" else "right")
    for s in doc["steps"]:
        table.add_row(
            s["step"],
            str(s["actions"]),
            f"[green]{s['hit']}[/green]" if s["hit"] else "0",
            f"[yellow]{s['miss']}[/yellow]" if s["miss"] else "0",
            str(s["unknown"]),
        )
    out.out.print(table)
    reasons = [(a["action_id"], r) for a in doc["actions"] if (r := _reason(a, base)) is not None]
    if reasons:
        out.line("why these actions run:")
        for action_id, reason in reasons[:MAX_REASONS]:
            out.line(f"  {action_id}: {reason}")
        if len(reasons) > MAX_REASONS:
            out.line(f"  … {len(reasons) - MAX_REASONS} more (see --json)")
    for action_id in doc["removed"]:
        out.line(f"  removed: {action_id}")


@handle_errors
def plan_command(
    ctx: typer.Context,
    steps: list[str] = STEPS_ARGUMENT,
    flow_file: Path = FLOW_OPTION,
    rehash: bool = typer.Option(
        False, "--rehash", help="Hash every source file (ignore git ids and the stat cache)."
    ),
    diff: str | None = typer.Option(
        None, "--diff", metavar="BUILD", help="Explain changes against this build (UUID or id)."
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Expand the flow; show actions per step, predicted cache hits and why misses rerun."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    result = make_plan(services, out, flow_file, targets=steps or (), rehash=rehash)
    store = services.store(required=False)
    prediction = predict(result.plan, store, result.planner) if store is not None else None
    record, old = _baseline(services, out, result, diff)
    new = prediction.plan if prediction else result.plan
    diffs = {d.action_id: d for d in diff_plans(old, new)} if old else {}
    removed = [d.action_id for d in diffs.values() if d.status == "removed"]
    doc = _document(result, prediction, record, diffs, removed)
    if json_mode:
        out.emit(doc)
    else:
        _print(out, doc)
