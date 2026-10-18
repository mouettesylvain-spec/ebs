"""`ebs debug ACTION`: what an action ran, with what, where, and how it ended (P1-10 R1).

Read only, from the build record and its events (state, reason, SLURM attempts), the build's
final plan in the CAS (`final_plan`, else `plan`: argv, declared env, toolchain, inputs with
their ids and origins), the metadata store when configured (`get_result`: exit code, first
error, runner host and job, log; `resolve_output`: the bytes of nondeterministic inputs) and
the debug area (`[debug].root`). Without `[metadata].url` the store's fields are null.

An action whose input ids were unknown when the record was written (a build that did not
finish) takes them from its recorded result (`ResultManifest.inputs`), so its key is complete.
"""

from __future__ import annotations

import dataclasses
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import typer

from ebs.cas.fs import FsCAS
from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli._records import BuildRecord, action_states, find_build
from ebs.cli.logs import match_action
from ebs.cli.plan import JSON_OPTION
from ebs.core.digest import Digest
from ebs.core.errors import CasError, ConfigError, MetadataError, PlanError
from ebs.driver.events import Event
from ebs.driver.retry import Attempt, attempt_history
from ebs.meta.api import MetadataStore, ResultManifest
from ebs.plan.keys import with_key
from ebs.plan.planfile import decode
from ebs.plan.types import ActionOutputInput, ActionSpec, ImportInput, InputRef, Plan, SourceInput
from ebs.runner.collect import debug_action_dir
from ebs.runner.errors import RunnerError
from ebs.runner.stage import resolve_content

__all__ = ["ActionTarget", "debug_command", "describe", "load_target"]


@dataclass(frozen=True)
class ActionTarget:
    """One action of a recorded build, with everything `debug` and `reproduce` read."""

    record: BuildRecord
    plan_digest: Digest
    plan: Plan
    spec: ActionSpec
    cas: FsCAS
    store: MetadataStore | None
    result: ResultManifest | None
    events: list[Event]


def load_target(services: SiteServices, action: str, build: str | None) -> ActionTarget:
    """Find the build (default: the latest here), the action (id or unique prefix), its plan."""
    record = find_build(services.cwd, build)
    action_id = match_action(action, record.actions)
    cas = services.cas(record.domain)
    digest = record.final_plan or record.plan
    try:
        with cas.open(digest) as f:
            plan = decode(f.read())
    except CasError as exc:
        raise CasError(
            f"the plan of build {record.uuid} ({digest}) is not in the CAS of domain "
            f"{record.domain!r}: {exc}; it may have been garbage-collected (`ebs pin` keeps a "
            "build's plan)"
        ) from exc
    except PlanError as exc:
        raise PlanError(f"the plan of build {record.uuid} ({digest}) is invalid: {exc}") from exc
    try:
        spec = plan.action(action_id)
    except KeyError:
        raise ConfigError(
            f"build {record.uuid} records action {action_id!r} but its plan {digest} has no "
            "such action; the build record is inconsistent"
        ) from None
    store = services.store(required=False)
    result = store.get_result(record.build, action_id) if store is not None else None
    if result is not None:
        spec = _with_result_ids(spec, result)
    return ActionTarget(
        record=record,
        plan_digest=digest,
        plan=plan,
        spec=spec,
        cas=cas,
        store=store,
        result=result,
        events=record.events(services.cwd),
    )


def _with_result_ids(spec: ActionSpec, result: ResultManifest) -> ActionSpec:
    if all(ref.id is not None for ref in spec.inputs):
        return spec
    inputs = tuple(
        dataclasses.replace(ref, id=result.inputs.get(ref.logical_path)) if ref.id is None else ref
        for ref in spec.inputs
    )
    return with_key(dataclasses.replace(spec, inputs=inputs, key=None))


# --- the description ----------------------------------------------------------------------------


def _origin(ref: InputRef) -> dict[str, str]:
    source = ref.source
    if isinstance(source, SourceInput):
        return {"type": "source", "input": source.input, "pattern": source.pattern}
    if isinstance(source, ActionOutputInput):
        return {"type": "action", "action_id": source.action_id, "output": source.output}
    assert isinstance(source, ImportInput)
    return {"type": "import", "name": source.name, "path": source.path}


def _content(target: ActionTarget, ref: InputRef) -> str | None:
    """The bytes staged for `ref` (its id, or for a nondeterministic output the recorded
    content); None when that needs the store or the id is unknown."""
    try:
        content = resolve_content(target.plan, target.spec, ref, target.store)
    except (RunnerError, MetadataError):
        return None
    return None if content is None else str(content)


def _last_event(target: ActionTarget, *types: str) -> Event | None:
    for event in reversed(target.events):
        if event.action_id == target.spec.action_id and event.type in types:
            return event
    return None


def _reason(state: str, finished: Event | None, attempts: Sequence[Attempt]) -> str | None:
    data = finished.data if finished is not None else {}
    given = data.get("reason") or data.get("hint")
    if isinstance(given, str) and given:
        return given
    if state == "failed":
        code = data.get("exit_code")
        return "the tool reported failure" + (f" (exit code {code})" if code is not None else "")
    if state == "infra_failed":
        failed = [a for a in attempts if a.reason is not None]
        last = failed[-1].reason if failed else "other"
        return f"infrastructure failure ({last}) after {len(attempts) or 1} attempt(s)"
    return None


def _summary(result: ResultManifest | None, name: str) -> str | None:
    value = None if result is None else result.summary.get(name)
    return value if isinstance(value, str) else None


def describe(services: SiteServices, target: ActionTarget) -> dict[str, Any]:
    """The `ebs debug --json` document (v1)."""
    spec, record, result = target.spec, target.record, target.result
    state = action_states([spec.action_id], target.events)[spec.action_id]
    finished = _last_event(target, "finished")
    attempts = attempt_history(target.events).get(spec.action_id, [])
    last = attempts[-1] if attempts else None
    exit_code = result.exit_code if result is not None else None
    if exit_code is None and finished is not None:
        code = finished.data.get("exit_code")
        exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
    log: str | None = None if result is None or result.log is None else str(result.log)
    if log is None:
        infra = _last_event(target, "infra_failed")
        raw = None if infra is None else infra.data.get("log")
        log = raw if isinstance(raw, str) else None
    debug_root = services.runner_settings().debug_root_for(record.domain)
    debug_dir = (
        None
        if debug_root is None
        else debug_action_dir(debug_root, record.domain, str(record.build), spec.action_id)
    )
    toolchain = spec.toolchain
    return {
        "v": 1,
        "uuid": str(record.uuid),
        "build": int(record.build),
        "action_id": spec.action_id,
        "step": spec.step,
        "state": state,
        "reason": _reason(state, finished, attempts),
        "exit_code": exit_code,
        "first_error": _summary(result, "first_error"),
        "signature": _summary(result, "signature"),
        "key": None if spec.key is None else str(spec.key),
        "argv": list(spec.argv),
        "command": shlex.join(spec.argv),
        "env": dict(sorted(spec.env.items())),
        "toolchain": None
        if toolchain is None
        else {"name": toolchain.name, "module": toolchain.module, "id": str(toolchain.id)},
        "inputs": [
            {
                "path": ref.logical_path,
                "kind": ref.kind,
                "id": None if ref.id is None else str(ref.id),
                "content": _content(target, ref),
                "origin": _origin(ref),
            }
            for ref in spec.inputs
        ],
        "slurm": {
            "job_id": (result.runner.slurm_job_id if result is not None else None)
            or (last.job_id if last is not None else None),
            "node": (result.runner.host if result is not None else None)
            or (last.node if last is not None else None),
        },
        "attempts": [
            {"attempt": a.attempt, "job_id": a.job_id, "node": a.node, "reason": a.reason}
            for a in attempts
        ],
        "debug_dir": None if debug_dir is None or not debug_dir.is_dir() else str(debug_dir),
        "log": log,
    }


def _origin_text(origin: dict[str, str]) -> str:
    if origin["type"] == "source":
        return f"source {origin['input']} ({origin['pattern']})"
    if origin["type"] == "action":
        return f"{origin['action_id']}.{origin['output']}"
    return f"import {origin['name']}:{origin['path']}"


def render(out: Output, doc: dict[str, Any]) -> None:
    def row(label: str, value: str) -> None:
        out.line(f"{label:<10} {value}")

    row("action", f"{doc['action_id']} (step {doc['step']})")
    row("build", f"{doc['uuid']} (id {doc['build']})")
    row("state", doc["state"] + (f": {doc['reason']}" if doc["reason"] else ""))
    if doc["signature"] or doc["first_error"]:
        row("error", doc["signature"] or doc["first_error"])
        if doc["signature"] and doc["first_error"]:
            row("", doc["first_error"])
    row("key", doc["key"] or "unknown (an input's producer has not run)")
    row("command", doc["command"])
    env = [f"{k}={v}" for k, v in doc["env"].items()] or ["(none declared)"]
    for i, line in enumerate(env):
        row("env" if i == 0 else "", line)
    tc = doc["toolchain"]
    row("toolchain", "none" if tc is None else f"{tc['name']} = {tc['module']} ({tc['id']})")
    row("inputs", f"{len(doc['inputs'])}")
    width = max((len(i["path"]) for i in doc["inputs"]), default=0)
    for i in doc["inputs"]:
        extra = ""
        if i["content"] is not None and i["content"] != i["id"]:
            extra = f" (content {i['content']})"
        out.line(
            f"  {i['path']:<{width}}  {i['kind']:<4}  {i['id'] or 'unknown'}  "
            f"from {_origin_text(i['origin'])}{extra}"
        )
    slurm = doc["slurm"]
    if slurm["job_id"] or slurm["node"]:
        row("slurm", f"job {slurm['job_id'] or '?'} on {slurm['node'] or '?'}")
    else:
        row("slurm", "never submitted")
    if len(doc["attempts"]) > 1 or any(a["reason"] for a in doc["attempts"]):
        for a in doc["attempts"]:
            row(
                "attempt",
                f"{a['attempt']}: job {a['job_id'] or '?'} on {a['node'] or '?'}"
                + (f" ({a['reason']})" if a["reason"] else ""),
            )
    row("debug dir", doc["debug_dir"] or "none (kept for failed actions, or debug.on_success)")
    row("log", f"ebs logs {shlex.quote(doc['action_id'])} --build {doc['uuid']}")
    row("reproduce", f"ebs reproduce {shlex.quote(doc['action_id'])} --build {doc['uuid']}")


@handle_errors
def debug_command(
    ctx: typer.Context,
    action: str = typer.Argument(..., metavar="ACTION", help="Action id or a unique prefix."),
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build UUID (or prefix) or id; default: the latest."
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show an action's state, error, command, env, toolchain, inputs, SLURM job and debug dir."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    doc = describe(services, load_target(services, action, build))
    if json_mode:
        out.emit(doc)
    else:
        render(out, doc)
