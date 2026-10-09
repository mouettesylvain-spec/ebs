"""Entry point of the `ebs` command."""

from __future__ import annotations

import logging
import sys
from typing import TextIO, cast

import typer

from ebs import __version__
from ebs.cli import build, logs, plan, rules, status
from ebs.cli._context import SiteServices
from ebs.cli._output import DEBUG_KEY
from ebs.core.log import configure_logging

app = typer.Typer(
    name="ebs",
    help="EDA Build System: hash-based builds of EDA flows on SLURM.",
    no_args_is_help=True,
    add_completion=False,
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
app.add_typer(rules.app, name="rules")
