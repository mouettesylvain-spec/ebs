"""`ebs rules list`: the step kinds this installation knows (entry points `ebs.rules`)."""

from __future__ import annotations

import typer
from rich.table import Table

from ebs.cli._context import SiteServices
from ebs.cli._output import handle_errors, make_output
from ebs.cli.plan import JSON_OPTION

__all__ = ["app"]

app = typer.Typer(help="Rule plugins (step kinds).", no_args_is_help=True)


@app.command("list")
@handle_errors
def list_command(ctx: typer.Context, json_mode: bool = JSON_OPTION) -> None:
    """List rule kinds with their versions (a version bump changes action keys)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    rules = [
        {"kind": p.kind, "version": p.version, "impl": f"{type(p).__module__}.{type(p).__name__}"}
        for p in services.rules().plugins()
    ]
    if json_mode:
        out.emit({"v": 1, "rules": rules})
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for name in ("KIND", "VERSION", "IMPLEMENTATION"):
        table.add_column(name)
    for r in rules:
        table.add_row(r["kind"], r["version"], r["impl"])
    out.out.print(table)
