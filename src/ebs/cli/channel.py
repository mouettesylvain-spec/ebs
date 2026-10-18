"""`ebs channel set|show|history` (P2-02 R3).

`set NAME CHANNEL VERSION` points `NAME:CHANNEL` at a release, after the channel's gate
(`[release.gates]`, `ebs.release.channels`) passes; with the metadata service, the service's
own gate policy applies. `history` lists every move with its author and gate check.

`--if-passing` makes `set` a CI gate job (P2-07 R4): the channel moves only if the gate build
(`--gate-build`, else `$EBS_BUILD_ID` from the `ebs build --ci` job's dotenv artifact) passed;
otherwise the command fails and the pipeline with it.
"""

from __future__ import annotations

from typing import Any

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import UsageError, handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.cli.release import DOMAIN_OPTION, default_domain, find_release, release_store
from ebs.meta.api import BuildId, ChannelMove
from ebs.release.channels import GatePolicy, move_channel, require_passing

__all__ = ["app"]

app = typer.Typer(help="Channels: movable pointers to releases (stable, nightly…).",
                  no_args_is_help=True)  # fmt: skip

NAME_ARGUMENT = typer.Argument(..., metavar="NAME", help="Release name, e.g. rtl-team/cpu.")
CHANNEL_ARGUMENT = typer.Argument(..., metavar="CHANNEL", help="Channel, e.g. stable.")


def _move_doc(m: ChannelMove) -> dict[str, Any]:
    return m.model_dump(mode="json")


def _move_text(m: ChannelMove) -> str:
    gate = "" if m.gate is None else f"  gate {m.gate.flow}: build {m.gate.build}"
    return f"{m.moved_at.isoformat()}  {m.version}  by {m.moved_by}{gate}"


def _env_build(value: str | None) -> BuildId:
    if not value:
        raise UsageError(
            "--if-passing needs the gate build: give --gate-build ID, or run after the "
            "`ebs build --ci` job so EBS_BUILD_ID comes from its dotenv artifact (ebs.env)"
        )
    if not value.strip().isdigit() or int(value) <= 0:
        raise UsageError(f"EBS_BUILD_ID={value!r} is not a build id (a positive integer)")
    return BuildId(int(value))


@app.command("set")
@handle_errors
def set_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    channel: str = CHANNEL_ARGUMENT,
    version: str = typer.Argument(..., metavar="VERSION", help="The release to point at."),
    gate_build: int | None = typer.Option(
        None, "--gate-build", metavar="ID", help="The gate build to check (default: any)."
    ),
    if_passing: bool = typer.Option(
        False,
        "--if-passing",
        help="CI gate: move only if the gate build (--gate-build, else $EBS_BUILD_ID) passed.",
    ),
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Point NAME:CHANNEL at release VERSION (after the channel's gate, if any)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    build = None if gate_build is None else BuildId(gate_build)
    if if_passing and build is None:
        build = _env_build(services.environ.get("EBS_BUILD_ID"))
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    gates = GatePolicy.from_section(services.config().get("release", {}))
    passed = require_passing(store, build, rel) if if_passing and build is not None else None
    move = move_channel(
        store, gates, rel, channel, by=services.user(), gate_build=build, passed=passed
    )
    if json_mode:
        out.emit(_move_doc(move))
    else:
        out.line(f"{name}:{channel} -> {version}")
        if move.gate is not None:
            out.line(f"gate {move.gate.flow}: passed by build {move.gate.build}")


@app.command("show")
@handle_errors
def show_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    channel: str = CHANNEL_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show the release a channel points at."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    target = default_domain(services, domain)
    found = release_store(services).get_channel(target, name, channel)
    if json_mode:
        out.emit({"v": 1, "channel": None if found is None else found.model_dump(mode="json")})
        return
    if found is None:
        out.line(f"{name}:{channel} is not set in domain {target!r}")
        return
    rel = found.release
    yanked = f"  (YANKED: {rel.yank_reason})" if rel.yanked_at is not None else ""
    out.line(f"{name}:{channel} -> {rel.version}{yanked}")
    out.line(f"moved {found.updated_at.isoformat()} by {found.updated_by}")


@app.command("history")
@handle_errors
def history_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    channel: str = CHANNEL_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Every move of a channel, oldest first, with its gate check."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    target = default_domain(services, domain)
    moves = release_store(services).channel_history(target, name, channel)
    if json_mode:
        out.emit({"v": 1, "moves": [_move_doc(m) for m in moves]})
        return
    if not moves:
        out.line(f"{name}:{channel} has never been set in domain {target!r}")
    for m in moves:
        out.line(_move_text(m))
