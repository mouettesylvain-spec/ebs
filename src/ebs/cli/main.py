"""Entry point of the `ebs` command."""

from __future__ import annotations

import typer

from ebs import __version__

app = typer.Typer(
    name="ebs",
    help="EDA Build System: hash-based builds of EDA flows on SLURM.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"ebs {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_print_version,
        is_eager=True,
        help="Show the ebs version and exit.",
    ),
) -> None:
    """EDA Build System."""
