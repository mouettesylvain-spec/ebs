"""`ebs lock update [ALIAS…]` and `ebs lock check` (P2-03 R4).

`update` resolves the flow's imports (channels and versions, in the flow's domain) with the
metadata store, prints what changes (old → new version, changed outputs) and rewrites
`flow.lock` next to the flow; with ALIAS arguments only those imports move. `check` resolves the
same way and exits 1 if `update` would change the file in any way (content or layout): the CI
guard that a reviewed lock is current. Neither runs or plans anything.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli.plan import FLOW_OPTION, JSON_OPTION, flow_path
from ebs.cli.release import release_store
from ebs.core.errors import ExitCode, ReleaseError
from ebs.flow.loader import load_flow
from ebs.flow.model import Flow
from ebs.release.lock import (
    LOCK_FILE,
    LockChange,
    LockFile,
    diff_locks,
    dump_lock,
    parse_lock,
    resolve_lock,
)

__all__ = ["app"]

app = typer.Typer(help="flow.lock: the exact releases the flow imports.", no_args_is_help=True)

ALIASES_ARGUMENT = typer.Argument(None, metavar="[ALIAS]...", help="Only these imports.")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read {path}: {exc}") from exc


def _load(services: SiteServices, flow_file: Path) -> tuple[Flow, Path, str | None]:
    path = flow_path(services, flow_file)
    flow = load_flow(path)
    services.check_domain_access(flow.domain)
    lock_path = path.parent / LOCK_FILE
    return flow, lock_path, _read(lock_path)


def _report(out: Output, changes: list[LockChange], written: bool, *, check: bool) -> None:
    if out.json:
        out.emit({"changes": [c.to_json() for c in changes], "written": written})
        return
    for change in changes:
        out.line(change.describe())
    if not changes and not check:
        out.line(f"{LOCK_FILE} is up to date" if not written else f"{LOCK_FILE} rewritten")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@app.command("update")
@handle_errors
def update_command(
    ctx: typer.Context,
    aliases: list[str] = ALIASES_ARGUMENT,
    flow_file: Path = FLOW_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Pin the flow's imports to the releases their channels or versions name now."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    flow, lock_path, text = _load(services, flow_file)
    old: LockFile | None = None
    if text is not None:
        try:
            old = parse_lock(text, file=str(lock_path))
        except ReleaseError as exc:
            out.warn(f"{exc}; resolving every import again")
            if aliases:
                raise ReleaseError(
                    f"{lock_path} is invalid, so the other imports have no pin to keep: run "
                    "`ebs lock update` without aliases"
                ) from exc
    if not flow.imports and text is None:
        _report(out, [], written=False, check=False)
        return
    new = resolve_lock(release_store(services), flow, old, aliases or ())
    rendered = dump_lock(new)
    written = rendered != text
    if written:
        _write(lock_path, rendered)
    _report(out, diff_locks(old, new), written, check=False)


@app.command("check")
@handle_errors
def check_command(
    ctx: typer.Context, flow_file: Path = FLOW_OPTION, json_mode: bool = JSON_OPTION
) -> None:
    """Exit 1 if `ebs lock update` would change flow.lock (for CI)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    flow, lock_path, text = _load(services, flow_file)
    if not flow.imports and text is None:
        _report(out, [], written=False, check=True)
        return
    old = None if text is None else parse_lock(text, file=str(lock_path))
    new = resolve_lock(release_store(services), flow, old)
    if dump_lock(new) == text:
        if not out.json:
            out.line(f"{LOCK_FILE} is up to date")
        else:
            _report(out, [], written=False, check=True)
        return
    changes = diff_locks(old, new)
    _report(out, changes, written=False, check=True)
    if not out.json:
        if text is None:
            out.line(f"no {lock_path}: run `ebs lock update` and commit it")
        elif not changes:
            out.line(
                f"{LOCK_FILE} pins the right releases but its layout differs from what "
                "`ebs lock update` writes; run it and commit the result"
            )
        else:
            out.line(f"{LOCK_FILE} is out of date: run `ebs lock update` and review the change")
    raise typer.Exit(int(ExitCode.ACTIONS_FAILED))
