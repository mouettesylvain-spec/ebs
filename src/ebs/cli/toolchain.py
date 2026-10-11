"""`ebs toolchain register|list|show|check`: the toolchain registry (P1-07).

`register` snapshots a module (`module purge; module load MODULE` in a clean shell), fingerprints
its install roots and stores it under an immutable id; `check` is the nightly drift report.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from ebs.cli._context import SiteServices
from ebs.cli._output import UsageError, handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.core.digest import Digest
from ebs.core.errors import DigestError, ExitCode
from ebs.meta.api import MetadataStore, ToolchainRecord
from ebs.toolchain.modules import detect_module_system, snapshot_module
from ebs.toolchain.registry import (
    CheckResult,
    FileChange,
    Registration,
    check,
    format_check,
    format_drift,
    register,
)

__all__ = ["app"]

app = typer.Typer(help="Registered toolchains (module snapshots).", no_args_is_help=True)

ROOT_OPTION = typer.Option(
    None,
    "--root",
    help="Extra install root to fingerprint (UVM, VIP dirs); repeatable.",
)
TIMEOUT_OPTION = typer.Option(120.0, "--timeout", min=1.0, help="Seconds allowed for module load.")
DAYS_OPTION = typer.Option(30, "--days", min=0, help="Check toolchains used in the last N days.")


def _store(services: SiteServices) -> MetadataStore:
    store = services.store(required=True)
    assert store is not None  # required=True raises instead
    return store


def _record_doc(r: ToolchainRecord) -> dict[str, Any]:
    return r.model_dump(mode="json")


def _changes_doc(changes: tuple[FileChange, ...], total: int) -> dict[str, Any]:
    return {"total": total, "files": [{"path": c.path, "change": c.change} for c in changes]}


@app.command("register")
@handle_errors
def register_command(
    ctx: typer.Context,
    module: str = typer.Argument(..., help="Module to snapshot, as <name>/<version>."),
    roots: list[Path] | None = ROOT_OPTION,
    timeout: float = TIMEOUT_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Snapshot MODULE (env, install roots, fingerprint) as an immutable toolchain."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = _store(services)
    system = detect_module_system(services.environ)
    snap = snapshot_module(
        module,
        system=system,
        environ=services.environ,
        user=services.user(),
        extra_roots=[r if r.is_absolute() else services.cwd / r for r in roots or []],
        timeout_s=timeout,
    )
    for message in snap.warnings:
        out.warn(message)
    reg = register(
        store,
        module=snap.module,
        version=snap.version,
        env=snap.env,
        install_roots=snap.install_roots,
        user=services.user(),
    )
    warning = format_drift(reg)
    if warning is not None:
        out.warn(warning)
    if json_mode:
        out.emit(_registration_doc(reg, system.flavour, system.version))
        return
    tc = reg.toolchain
    out.line(f"{reg.status} {tc.module}")
    out.line(f"  id             {tc.id}")
    out.line(f"  fingerprint    {tc.fingerprint}")
    out.line(f"  install roots  {', '.join(str(r) for r in tc.install_roots)}")
    out.line(f"  env            {len(tc.env)} variable(s): {', '.join(tc.env)}")
    out.line(f"  module system  {system.flavour} {system.version}")


def _registration_doc(reg: Registration, flavour: str, version: str) -> dict[str, Any]:
    tc = reg.toolchain
    return {
        "v": 1,
        "status": reg.status,
        "id": str(tc.id),
        "module": tc.module,
        "version": tc.version,
        "fingerprint": str(tc.fingerprint),
        "install_roots": [str(r) for r in tc.install_roots],
        "env": dict(tc.env),
        "previous": None if reg.previous is None else str(reg.previous),
        "superseded": [str(d) for d in reg.superseded],
        "drift": None
        if not reg.drifted
        else {
            **_changes_doc(reg.changes, reg.changes_total),
            "env": list(reg.env_changes),
        },
        "module_system": {"flavour": flavour, "version": version},
    }


@app.command("list")
@handle_errors
def list_command(
    ctx: typer.Context,
    all_: bool = typer.Option(False, "--all", help="Include superseded toolchains."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """List the current toolchain of every registered module."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    rows = _store(services).list_toolchains(current_only=not all_)
    if json_mode:
        out.emit({"v": 1, "toolchains": [_record_doc(r) for r in rows]})
        return
    table = Table(box=None, pad_edge=False, header_style="bold")
    for name in ("MODULE", "ID", "REGISTERED", "BY", "LAST USED", "SUPERSEDED BY"):
        table.add_column(name)
    for r in rows:
        table.add_row(
            r.module,
            str(r.id),
            r.registered_at.isoformat(timespec="seconds"),
            r.registered_by,
            "-" if r.last_used_at is None else r.last_used_at.isoformat(timespec="seconds"),
            "-" if r.superseded_by is None else str(r.superseded_by),
        )
    out.out.print(table)


@app.command("show")
@handle_errors
def show_command(
    ctx: typer.Context,
    which: str = typer.Argument(..., help="A toolchain id (sha256:…) or a module name."),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show one toolchain: a module's current one, or any id (superseded ones too)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = _store(services)
    if ":" in which and "/" not in which:
        try:
            record = store.get_toolchain(Digest.parse(which))
        except DigestError as exc:
            raise UsageError(f"{which!r} is not a toolchain id: {exc}") from None
    else:
        record = store.current_toolchain(which)
    if record is None:
        raise UsageError(
            f"no registered toolchain {which!r}; see `ebs toolchain list --all`, or register it "
            "with `ebs toolchain register <module>`"
        )
    if json_mode:
        out.emit({"v": 1, "toolchain": _record_doc(record)})
        return
    for key, value in _record_doc(record).items():
        if key == "env":
            out.line("env:")
            for name, val in value.items():
                out.line(f"  {name}={val}")
        elif key == "install_roots":
            out.line("install_roots:")
            for root in value:
                out.line(f"  {root}")
        else:
            out.line(f"{key}: {'-' if value is None else value}")


@app.command("check")
@handle_errors
def check_command(
    ctx: typer.Context,
    days: int = DAYS_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Re-fingerprint recently used toolchains and report drift (exit 1 if any); changes nothing."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    results = check(_store(services), days=days, now=services.clock.now())
    if json_mode:
        out.emit({"v": 1, "days": days, "results": [_check_doc(r) for r in results]})
    else:
        if not results:
            out.line(f"no toolchain used in the last {days} day(s)")
        for result in results:
            for line in format_check(result):
                out.line(line)
    if any(r.status != "ok" for r in results):
        raise typer.Exit(int(ExitCode.ACTIONS_FAILED))


def _check_doc(r: CheckResult) -> dict[str, Any]:
    return {
        "id": str(r.record.id),
        "module": r.record.module,
        "status": r.status,
        "fingerprint": None if r.fingerprint is None else str(r.fingerprint),
        "registered_fingerprint": str(r.record.fingerprint),
        "changes": _changes_doc(r.changes, r.changes_total),
        "error": r.error,
    }
