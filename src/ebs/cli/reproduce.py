"""`ebs reproduce ACTION`: rebuild an action's exact sandbox and run it, or open a shell in it
(P1-10 R2-R5).

The sandbox comes from `ebs.runner.reproduce` (the runner's own staging and env code) in
`--dir D` (new or empty) or `<[scratch].dir>/repro/<action dir>`, and is kept. Then:

- nothing more: print where it is and how to go on;
- `--run`: run the command as the runner would (log in `logs/tool.log`), classify the exit with
  the action's rule, and compare every deterministic output with the recorded result (R3); exit
  1 when an output differs from it, or is missing, so CI can use the comparison directly;
- `--interactive`: `bash --norc` in `work/` with the sandbox env (and a `PS1` naming the
  action), e.g. to start the tool's GUI on a failing test.

`--salloc` (with `--run` or `--interactive`) runs `salloc <the action's job options> srun --pty
ebs reproduce ACTION --build <uuid> …` instead: the allocation gets the action's cpus, memory,
time (+ the runner's grace; `--time` overrides it, e.g. for a GUI session), licenses, account,
QOS, partition and constraint, rendered like the SLURM executor's (`job_options`, P1-03), and
the inner command builds the sandbox on the node's local disk. Reproduce never posts anything:
no build, result, cache entry or event (R4).
"""

from __future__ import annotations

import math
import shlex
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

import typer
from pydantic import ValidationError

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, UsageError, handle_errors, make_output
from ebs.cli.debug import ActionTarget, load_target
from ebs.cli.plan import JSON_OPTION
from ebs.core.errors import CasError, ConfigError, EbsError, ExitCode
from ebs.exec.slurm.accounts import JobPolicy, job_constraint
from ebs.exec.slurm.cli import SbatchOptions, job_options
from ebs.exec.slurm.executor import JOB_TIME_GRACE_S, SlurmSettings
from ebs.exec.slurm.licenses import batch_licenses, license_request
from ebs.flow.model import parse_duration
from ebs.plan.types import ActionSpec
from ebs.runner.collect import log_tail, output_paths
from ebs.runner.errors import InfraError, InputVerificationError, RunnerError
from ebs.runner.errors import UsageError as RunnerUsageError
from ebs.runner.reproduce import (
    OutputCheck,
    Sandbox,
    build_sandbox,
    check_outputs,
    identical,
    run_sandbox,
)
from ebs.runner.run import ToolRun

__all__ = ["cli_error", "reproduce_command", "salloc_argv"]

_MIB: Final = 1 << 20
DIR_OPTION = typer.Option(
    None, "--dir", metavar="DIR", help="Build the sandbox here (new or empty directory)."
)


def _number(spec: ActionSpec, name: str) -> int | None:
    value = getattr(spec.resources, name)
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise ConfigError(
        f"action {spec.action_id}: resources.{name} is {value!r}, not a number; the plan must "
        "hold expanded resources (re-run `ebs build`)"
    )


def salloc_argv(
    spec: ActionSpec,
    *,
    settings: SlurmSettings,
    project: str,
    inner: Sequence[str],
    time_s: int | None = None,
) -> list[str]:
    """`salloc <job options> srun --pty <inner>` with the options the SLURM executor would use
    for `spec` alone (P1-02/P1-03), each one `--name=value` element; `time_s` replaces the
    action's time limit (and its grace)."""

    def command(name: str) -> str:
        return str(settings.bin_dir / name) if settings.bin_dir is not None else name

    policy = JobPolicy.resolve(
        account_map=settings.account_map,
        qos=settings.qos,
        os_constraint=settings.os_constraint,
        project=project,
        build_kind="dev",
    )
    cpus, mem, time = _number(spec, "cpus"), _number(spec, "mem"), _number(spec, "time")
    licenses = license_request(
        batch_licenses([spec]), server=settings.license_server, fuzzy=settings.license_fuzzy
    )
    constraint = job_constraint(
        policy.os_constraint,
        [] if spec.resources.constraint is None else [spec.resources.constraint],
    )
    if time_s is None and time is not None:
        time_s = time + JOB_TIME_GRACE_S
    try:
        opts = SbatchOptions(
            job_name=f"ebs:repro:{spec.step}",
            partition=settings.default_partition,
            account=policy.account,
            qos=policy.qos,
            cpus_per_task=cpus,
            mem_mb=None if mem is None else max(1, math.ceil(mem / _MIB)),
            time_limit_s=time_s,
            licenses=licenses,
            constraint=constraint,
        )
    except ValidationError as exc:  # a [slurm] value or a step resource SLURM cannot take
        problems = "; ".join(f"{e['loc'][0]}: {e['msg']}" for e in exc.errors())
        raise ConfigError(
            f"action {spec.action_id}: cannot ask salloc for its allocation ({problems}); fix "
            "the [slurm] settings or the step's resources"
        ) from None
    return [command("salloc"), *job_options(opts), command("srun"), "--pty", *inner]


def cli_error(exc: RunnerError) -> EbsError:
    """Runner errors as the CLI's: usage (exit 2) or infrastructure (exit 3)."""
    if isinstance(exc, RunnerUsageError):
        return UsageError(str(exc))
    if isinstance(exc, InfraError | InputVerificationError):
        return CasError(str(exc))
    return exc


def _status(
    services: SiteServices,
    spec: ActionSpec,
    run: ToolRun,
    sandbox: Sandbox,
    checks: Sequence[OutputCheck],
) -> str:
    """What the runner would conclude (`Runner._finish`): passed / failed (cacheable) /
    infra (<reason>); a missing required output makes a pass `failed`."""
    if run.exec_error is not None:
        return "infra (the program could not start)"
    if run.log_error is not None:
        return "infra (the log could not be written)"
    if run.timed_out:
        return "infra (timeout)"
    rule = services.rules().get(spec.rule.kind)
    work = sandbox.scratch.work
    log = sandbox.scratch.logs / "tool.log"
    classification = rule.classify(run.exit_code, log_tail(log), output_paths(spec, work))
    if classification.status == "infra":
        return f"infra ({classification.reason or 'other'})"
    required = {o.name for o in spec.outputs if not o.optional}
    if any(c.status == "missing" and c.name in required for c in checks):
        return "failed"
    return classification.status


def _check_doc(check: OutputCheck) -> dict[str, Any]:
    return {
        "name": check.name,
        "status": check.status,
        "recorded": None if check.recorded is None else str(check.recorded),
        "reproduced": None if check.reproduced is None else str(check.reproduced),
    }


def _run(services: SiteServices, target: ActionTarget, sandbox: Sandbox) -> dict[str, Any]:
    spec = target.spec
    run = run_sandbox(spec, sandbox, services.runner_settings())
    recorded = None if target.result is None else target.result.outputs
    checks = check_outputs(spec, sandbox.scratch.work, recorded)
    return {
        "exit_code": run.exit_code,
        "status": _status(services, spec, run, sandbox, checks),
        "timed_out": run.timed_out,
        "error": run.exec_error,
        "log": str(sandbox.scratch.logs / "tool.log"),
        "outputs": [_check_doc(c) for c in checks],
        "identical": identical(checks) if target.result is not None else None,
    }


def _shell(services: SiteServices, spec: ActionSpec, sandbox: Sandbox) -> int:
    env = dict(sandbox.env)
    bash = shutil.which("bash", path=env.get("PATH")) or "/bin/bash"
    env["PS1"] = f"[ebs repro {spec.action_id}] \\w\\$ "
    return services.run_foreground([bash, "--norc"], cwd=sandbox.scratch.work, env=env)


def _render(out: Output, doc: dict[str, Any], sandbox: Sandbox, spec: ActionSpec) -> None:
    out.line(f"sandbox for {doc['action_id']} (build {doc['uuid']}) in {doc['dir']}")
    out.line(
        f"staged {len(spec.inputs)} input(s) and {len(spec.config_files)} config file(s) in "
        f"{doc['work']}, every digest verified; env: {len(sandbox.env)} variable(s)"
    )
    out.line(f"command: {shlex.join(spec.argv)}")
    ran = doc["ran"]
    if ran is not None:
        out.line(f"ran: exit code {ran['exit_code']} ({ran['status']}); log {ran['log']}")
        for check in ran["outputs"]:
            match check["status"]:
                case "same":
                    out.line(f"  {check['name']}: same as recorded ({check['reproduced']})")
                case "different":
                    out.line(
                        f"  {check['name']}: differs (recorded {check['recorded']}, "
                        f"reproduced {check['reproduced']})"
                    )
                case "missing":
                    out.line(f"  {check['name']}: not produced")
                case "not_compared":
                    out.line(f"  {check['name']}: deterministic: false, not compared")
                case _:
                    out.line(f"  {check['name']}: produced, but not in the recorded result")
    elif not doc["interactive"]:
        out.line(
            f"next: `ebs reproduce {shlex.quote(doc['action_id'])} --interactive` for a shell "
            "with this env, or --run to run the command"
        )
    out.line(f"the sandbox is kept; remove it with: rm -rf {shlex.quote(doc['dir'])}")


@handle_errors
def reproduce_command(
    ctx: typer.Context,
    action: str = typer.Argument(..., metavar="ACTION", help="Action id or a unique prefix."),
    build: str | None = typer.Option(
        None, "--build", metavar="BUILD", help="Build UUID (or prefix) or id; default: the latest."
    ),
    directory: Path | None = DIR_OPTION,
    run: bool = typer.Option(False, "--run", help="Run the command and compare the outputs."),
    interactive: bool = typer.Option(
        False, "--interactive", help="Open `bash --norc` in the sandbox with its env."
    ),
    salloc: bool = typer.Option(
        False, "--salloc", help="Do it on a compute node, in an allocation like the action's job."
    ),
    time: str | None = typer.Option(
        None,
        "--time",
        metavar="DURATION",
        help="With --salloc: the allocation's time limit (e.g. 4h) instead of the action's.",
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Rebuild an action's sandbox exactly as the runner did; run it or open a shell in it."""
    if run and interactive:
        raise UsageError("give --run or --interactive, not both")
    if salloc and not (run or interactive):
        raise UsageError("--salloc needs --run or --interactive (what to do on the node)")
    if time is not None and not salloc:
        raise UsageError("--time sets the --salloc allocation's time limit; add --salloc")
    try:
        time_s = None if time is None else parse_duration(time)
    except ValueError as exc:
        raise UsageError(f"--time: {exc}") from None
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    target = load_target(services, action, build)
    spec, record = target.spec, target.record
    if salloc:
        inner = [
            *services.ebs_argv(), "reproduce", spec.action_id, "--build", str(record.uuid),
            "--run" if run else "--interactive",
            *(["--dir", str(directory)] if directory is not None else []),
            *(["--json"] if json_mode else []),
        ]  # fmt: skip
        argv = salloc_argv(
            spec,
            settings=SlurmSettings.from_config(services.config()),
            project=target.plan.project,
            inner=inner,
            time_s=time_s,
        )
        # The inner `ebs reproduce` prints the result (as JSON with --json) on this stdout.
        out.err.print(f"ebs: running {shlex.join(argv)}", markup=False)
        code = services.run_foreground(argv, cwd=None, env=None)
        if code != 0:
            raise typer.Exit(code)
        return
    try:
        sandbox = build_sandbox(
            target.plan,
            spec,
            cas=target.cas,
            store=target.store,
            settings=services.runner_settings(),
            caller_env=services.environ,
            dest=None if directory is None else directory.absolute(),
            record={
                "build": str(record.uuid),
                "build_id": int(record.build),
                "plan": str(target.plan_digest),
            },
        )
    except RunnerError as exc:
        raise cli_error(exc) from exc
    doc: dict[str, Any] = {
        "v": 1,
        "uuid": str(record.uuid),
        "build": int(record.build),
        "action_id": spec.action_id,
        "dir": str(sandbox.scratch.root),
        "work": str(sandbox.scratch.work),
        "argv": list(spec.argv),
        "env": dict(sorted(sandbox.env.items())),
        "interactive": interactive,
        "ran": _run(services, target, sandbox) if run else None,
    }
    if json_mode:
        out.emit(doc)
    else:
        _render(out, doc, sandbox, spec)
    if interactive:
        _shell(services, spec, sandbox)
    elif doc["ran"] is not None and doc["ran"]["identical"] is False:
        raise typer.Exit(int(ExitCode.ACTIONS_FAILED))  # an output differs from the record
