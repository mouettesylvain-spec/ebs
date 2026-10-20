"""`ebs discover STEP`: run one instance of a step under strace and list what it reads (P3-02).

The flow is planned (refined through cache hits, so upstream outputs that exist can be staged),
one instance of STEP is chosen (`--row k=v` narrows a matrix; the first match in plan order runs)
and staged with the runner's own code (`ebs.runner.reproduce.build_sandbox`: strict staging, no
namespace sandbox, nothing posted). The command runs under `strace -f -e trace=file,process` in
the work dir; `ebs.sandbox.discover` classifies every file it read and proposes an `inputs:` patch
for the step. The flow file is changed only with `--write`; `--audit` exits 1 when anything was
read that is not declared (for nightly jobs). The scratch is removed unless `--keep`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import FrameType
from typing import Any
from urllib.parse import unquote

import typer
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli.plan import FLOW_OPTION, JSON_OPTION, make_plan, predict
from ebs.cli.reproduce import cli_error
from ebs.core.errors import ExitCode, FlowError, SandboxError
from ebs.flow.loader import load_flow
from ebs.plan.types import ActionSpec, Plan, SourceInput
from ebs.runner.errors import RunnerError
from ebs.runner.reproduce import Sandbox, build_sandbox, run_sandbox
from ebs.runner.stage import remove_tree
from ebs.sandbox.discover import (
    Layout,
    Proposal,
    Report,
    analyze,
    parse_strace,
    propose_inputs,
    render_patch,
    strace_argv,
    yaml_dumper,
)

__all__ = ["discover_command", "select_action"]

TRACE_LOG = "strace.log"
ROW_OPTION = typer.Option(
    [], "--row", metavar="COLUMN=VALUE", help="Pick the matrix instance with this value."
)


def _row(action_id: str) -> dict[str, str]:
    """The matrix row an instance id names (`step[k=v,…]`, percent-encoded)."""
    if not action_id.endswith("]") or "[" not in action_id:
        return {}
    pairs = action_id[action_id.index("[") + 1 : -1]
    row: dict[str, str] = {}
    for pair in filter(None, pairs.split(",")):
        key, _, value = pair.partition("=")
        row[unquote(key)] = unquote(value)
    return row


def select_action(plan: Plan, step: str, rows: Sequence[str]) -> tuple[ActionSpec, int]:
    """The first instance of `step` (plan order) whose row has every `k=v` of `rows`, and how
    many instances matched."""
    wanted: dict[str, str] = {}
    for item in rows:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise UsageError(f"--row {item!r}: expected COLUMN=VALUE")
        wanted[key] = value
    instances = [a for a in plan.actions if a.step == step]
    if not instances:
        steps = sorted({a.step for a in plan.actions})
        raise UsageError(f"the flow has no step {step!r}; steps: {', '.join(steps)}")
    matches = [a for a in instances if wanted.items() <= _row(a.action_id).items()]
    if not matches:
        sample = ", ".join(a.action_id for a in instances[:5])
        raise UsageError(
            f"no instance of {step!r} has {' '.join(rows)}; instances include {sample}"
        )
    return matches[0], len(matches)


@contextlib.contextmanager
def _sigterm_raises() -> Iterator[None]:
    """SIGTERM raises KeyboardInterrupt (exit 130) so `finally` blocks remove the scratch; the
    tool's process group is killed by `run_tool` on the way out."""

    def handler(signum: int, frame: FrameType | None) -> None:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # the cleanup is not interrupted itself
        raise KeyboardInterrupt(f"signal {signum}")

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _plain(value: object) -> object:
    """ruamel round-trip data as plain dicts, lists and scalars."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def _load_yaml(path: Path) -> Any:
    yaml = YAML(typ="rt", pure=True)
    yaml.preserve_quotes = True
    try:
        return yaml.load(path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        raise FlowError(f"cannot read the flow {path}: {exc}", file=str(path)) from exc


def _step_inputs(doc: Any, step: str) -> dict[str, object]:
    raw = doc["steps"][step].get("inputs") or {}
    plain = _plain(raw)
    return plain if isinstance(plain, dict) else {}


def _write(path: Path, step: str, proposal: Proposal) -> None:
    """Replace the step's `inputs:` in the flow file (comments elsewhere kept); the new flow must
    load, else nothing is changed."""
    doc = _load_yaml(path)
    body = doc["steps"][step]
    if proposal.inputs:
        body["inputs"] = proposal.inputs
    elif "inputs" in body:
        del body["inputs"]
    yaml = yaml_dumper()
    yaml.preserve_quotes = True
    tmp = path.with_name(f".{path.name}.discover-{os.getpid()}")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            yaml.dump(doc, f)
        load_flow(tmp)  # the patched flow is still valid
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _layout(
    services: SiteServices, plan: Plan, spec: ActionSpec, sandbox: Sandbox, flow_dir: Path
) -> Layout:
    def real(path: Path) -> str:
        return os.path.realpath(path)

    def both(paths: Sequence[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(p for path in paths for p in (path, os.path.realpath(path))))

    settings = services.runner_settings()
    roots: tuple[str, ...] = ()
    if spec.toolchain is not None and spec.toolchain.name in plan.toolchains:
        roots = plan.toolchains[spec.toolchain.name].install_roots
    # strace reports paths through the real cwd (-y), so the scratch and flow dir are resolved.
    return Layout(
        work=real(sandbox.scratch.work),
        scratch=real(sandbox.scratch.root),
        flow_dir=real(flow_dir),
        inputs=frozenset(
            {r.logical_path for r in spec.inputs if r.kind == "file"} | set(spec.config_files)
        ),
        trees=tuple(r.logical_path for r in spec.inputs if r.kind == "tree"),
        staged=both([str(settings.cas_root_for(spec.domain))]),
        toolchain=both([*roots, *settings.sandbox_ro_binds]),
        outputs=tuple(o.path for o in spec.outputs),
        # $HOME, $TMPDIR and $PWD reach the tool unresolved; Makefiles name the flow dir as is.
        aliases=tuple(
            (str(p), real(p)) for p in (sandbox.scratch.root, flow_dir) if str(p) != real(p)
        ),
    )


def _render(out: Output, doc: dict[str, Any], report: Report) -> None:
    counts = ", ".join(f"{n} {c}" for c, n in doc["counts"].items())
    out.line(
        f"discover {doc['action_id']}: exit code {doc['exit_code']}; "
        f"{len(report.reads)} file(s) read: {counts}"
    )
    if doc["undeclared"]:
        out.line("undeclared reads (first reader):")
        for u in doc["undeclared"]:
            note = "; read failed: not staged" if u["missing"] else ""
            out.line(f"  {u['path']}  (pid {u['pid']} {u['process'] or '?'}{note})")
    else:
        out.line("no undeclared reads")
    out.line("")
    out.out.print(doc["patch"], end="", markup=False, highlight=False, soft_wrap=True)
    if doc["written"]:
        out.line(f"wrote the proposed inputs into {doc['flow']}")
    if doc["dir"] is not None:
        out.line(f"kept the sandbox and trace in {doc['dir']}")


@handle_errors
def discover_command(
    ctx: typer.Context,
    step: str = typer.Argument(..., metavar="STEP", help="The step to trace."),
    row: list[str] = ROW_OPTION,
    flow_file: Path = FLOW_OPTION,
    audit: bool = typer.Option(
        False, "--audit", help="Exit 1 if the step reads anything it does not declare."
    ),
    write: bool = typer.Option(
        False, "--write", help="Write the proposed inputs into the flow file."
    ),
    keep: bool = typer.Option(False, "--keep", help="Keep the sandbox and the strace log."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Run one instance of STEP under strace; list undeclared reads and propose its inputs."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    strace = shutil.which("strace", path=services.environ.get("PATH"))
    if strace is None:
        raise SandboxError(
            "ebs discover needs strace, which is not on PATH; install it (package 'strace') "
            "or add its directory to PATH"
        )
    result = make_plan(services, out, flow_file, targets=[step])
    store = services.store(required=False)
    plan = predict(result.plan, store, result.planner).plan if store else result.plan
    spec, matched = select_action(plan, step, row)
    if matched > 1:
        out.warn(f"{matched} instances of {step!r} match; tracing the first, {spec.action_id}")
    if result.flow.steps[step].matrix is not None and write:
        out.warn("the proposal comes from one matrix instance; check it fits every row")
    pending = sorted({r.logical_path for r in spec.inputs if r.id is None})
    if pending:
        raise UsageError(
            f"{spec.action_id}: input(s) {', '.join(pending)} come from steps that have not run "
            f"(or missed the cache); run `ebs build {step}` first, then discover"
        )
    with _sigterm_raises():  # a killed CI/nightly job still removes its scratch (I16)
        settings = services.runner_settings()
        try:
            sandbox = build_sandbox(
                plan, spec, cas=result.cas, store=store, settings=settings,
                caller_env=services.environ, record={"discover": step},
            )  # fmt: skip
        except RunnerError as exc:
            raise cli_error(exc) from exc
        try:
            trace = sandbox.scratch.logs / TRACE_LOG
            traced = replace(spec, argv=tuple(strace_argv(strace, str(trace), spec.argv)))
            run = run_sandbox(traced, sandbox, settings)
            if run.exec_error is not None:
                raise SandboxError(f"cannot start strace: {run.exec_error}")
            try:
                text = trace.read_text(encoding="utf-8", errors="surrogateescape")
            except FileNotFoundError:
                text = ""
            if not text.strip():
                log = (sandbox.scratch.logs / "tool.log").read_text(errors="replace")[:500]
                log = log.strip() or "no output"
                raise SandboxError(
                    f"strace wrote no trace (exit code {run.exit_code}): {log}"
                    "; check that ptrace is allowed (kernel.yama.ptrace_scope)"
                )
            layout = _layout(services, plan, spec, sandbox, result.path.parent)
            report = analyze(parse_strace(text, cwd=layout.work), layout)
            raw = _step_inputs(_load_yaml(result.path), step)
            sources = {
                r.logical_path: r.source.input
                for r in spec.inputs
                if isinstance(r.source, SourceInput)
            }
            partial = run.timed_out or run.exit_code != 0
            proposal = propose_inputs(raw, sources, report, layout, partial=partial)
            if write:
                _write(result.path, step, proposal)
            doc: dict[str, Any] = {
                "v": 1,
                "flow": str(result.path),
                "step": step,
                "action_id": spec.action_id,
                "exit_code": run.exit_code,
                "timed_out": run.timed_out,
                "dir": str(sandbox.scratch.root) if keep else None,
                "counts": report.counts(),
                "undeclared": [
                    {
                        "path": r.path,
                        "pid": r.pid,
                        "process": r.process,
                        "syscall": r.syscall,
                        "missing": r.missing,
                    }
                    for r in report.undeclared()
                ],
                "proposal": {
                    "inputs": proposal.inputs,
                    "unused": list(proposal.unused),
                    "added": proposal.added,
                    "outside": list(proposal.outside),
                },
                "patch": render_patch(step, spec.action_id, proposal),
                "written": write,
            }
        finally:
            if not keep:
                remove_tree(sandbox.scratch.root)
    if run.timed_out or run.exit_code != 0:
        out.warn(
            f"{spec.action_id} exited with {run.exit_code}"
            f"{' (timed out)' if run.timed_out else ''}; the trace covers what it read until then"
        )
    if json_mode:
        out.emit(doc)
    else:
        _render(out, doc, report)
    if audit and partial:
        out.warn("audit incomplete: the step did not finish, so not everything it reads was seen")
    if audit and (doc["undeclared"] or partial):
        raise typer.Exit(int(ExitCode.ACTIONS_FAILED))
