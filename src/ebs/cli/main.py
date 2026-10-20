"""Entry point of the `ebs` command."""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import typer

from ebs import __version__
from ebs.cli import (
    build,
    channel,
    discover,
    domain,
    gc,
    lock,
    logs,
    outputs,
    plan,
    release,
    reproduce,
    rules,
    seeds,
    status,
    tests_cmd,
    toolchain,
    used_by,
    why,
)
from ebs.cli import debug as debug_cli
from ebs.cli._context import SiteServices
from ebs.cli._output import DEBUG_KEY
from ebs.core.log import configure_logging

app = typer.Typer(
    name="ebs",
    help="EDA Build System: hash-based builds of EDA flows on SLURM.",
    no_args_is_help=True,
    add_completion=True,  # --install-completion / --show-completion (site: Shell and Make)
)


class _CurrentStderr:
    """Writes to whatever `sys.stderr` is at write time.

    Logging is configured once per invocation; binding it to the stream object of that moment
    would keep writing to a stream that is later replaced and closed (a test runner's capture).
    """

    def write(self, text: str) -> int:
        return sys.stderr.write(text)

    def flush(self) -> None:
        sys.stderr.flush()


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"ebs {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Show the ebs version and exit.",
    ),
    debug: bool = typer.Option(False, "--debug", help="Show tracebacks and debug logs on stderr."),
) -> None:
    """EDA Build System."""
    ctx.meta[DEBUG_KEY] = debug
    configure_logging(
        logging.DEBUG if debug else logging.WARNING, stream=cast(TextIO, _CurrentStderr())
    )
    if not isinstance(ctx.obj, SiteServices):  # tests pass their own
        ctx.obj = SiteServices.from_process()
    ctx.call_on_close(ctx.obj.close)


app.command("plan")(plan.plan_command)
app.command("build")(build.build_command)
app.command("status")(status.status_command)
app.command("logs")(logs.logs_command)
app.command("debug")(debug_cli.debug_command)
app.command("reproduce")(reproduce.reproduce_command)
app.command("discover")(discover.discover_command)
app.command("outputs")(outputs.outputs_command)
app.command("fetch")(outputs.fetch_command)
app.command("gc")(gc.gc_command)
app.command("pin")(gc.pin_command)
app.command("unpin")(gc.unpin_command)
app.command("why")(why.why_command)
app.command("used-by")(used_by.used_by_command)
app.command("tests")(tests_cmd.list_tests_command)
app.add_typer(domain.app, name="domain")
app.add_typer(release.app, name="release")
app.add_typer(channel.app, name="channel")
app.add_typer(lock.app, name="lock")
app.add_typer(rules.app, name="rules")
app.add_typer(seeds.app, name="seeds")
app.add_typer(toolchain.app, name="toolchain")
