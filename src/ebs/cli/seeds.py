"""`ebs seeds gen TABLE`: write explicit seeds into a parameter table (task P1-11).

The algorithm and formatting rules are in `ebs.flow.seeds`.
"""

from __future__ import annotations

from pathlib import Path

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.flow.seeds import SeedOptions, write_seeds

__all__ = ["app"]

app = typer.Typer(help="Seeds in parameter tables.", no_args_is_help=True)

TABLE_ARGUMENT = typer.Argument(..., help="CSV or YAML table to edit in place.")


@app.command("gen")
@handle_errors
def gen_command(
    ctx: typer.Context,
    table: Path = TABLE_ARGUMENT,
    per_test: int | None = typer.Option(
        None, "--per-test", help="Append N rows per distinct test, each with a new seed."
    ),
    column: str = typer.Option("seed", "--column", help="Seed column."),
    test_column: str = typer.Option("test", "--test-column", help="Column naming the test."),
    base_seed: int = typer.Option(
        0, "--base-seed", help="Seed of the generator (random.Random); same value, same seeds."
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Fill rows whose seed cell is empty; other rows stay as they are."
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Generate seeds into TABLE (31-bit positive ints, deterministic, no duplicate test/seed)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    path = table if table.is_absolute() else services.cwd / table
    options = SeedOptions(
        per_test=per_test,
        column=column,
        test_column=test_column,
        base_seed=base_seed,
        replace=replace,
    )
    result = write_seeds(path, options)
    if json_mode:
        out.emit(
            {
                "v": 1,
                "table": str(table),
                "appended": result.appended,
                "filled": result.filled,
                "rows": [{"change": c.change, "row": dict(c.row)} for c in result.changes],
            }
        )
        return
    out.line(f"{table}: {result.appended} rows appended, {result.filled} empty seeds filled")
    for c in result.changes:  # to review before committing the table
        cells = " ".join(f"{k}={v}" for k, v in c.row.items())
        out.line(f"  {c.change:<8} {cells}")
