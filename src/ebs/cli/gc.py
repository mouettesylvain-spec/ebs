"""`ebs gc`, `ebs pin`, `ebs unpin` (P1-09).

`ebs gc [--dry-run] [--domain D]` collects one domain: it runs as the GC service account (member
of the domain's group, with direct access to the metadata database), usually from the systemd
timer in deploy/gc/. `ebs pin BUILD` makes a build's plan, outputs and logs GC roots;
`ebs unpin BUILD` removes the pin.
"""

from __future__ import annotations

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import handle_errors, make_output
from ebs.cli._records import find_build
from ebs.cli.plan import JSON_OPTION
from ebs.core.errors import ConfigError
from ebs.gc.report import render
from ebs.gc.run import collect
from ebs.gc.sweep import GcConfig
from ebs.meta.api import BuildId

__all__ = ["gc_command", "pin_command", "unpin_command"]

BUILD_ARGUMENT = typer.Argument(
    ..., metavar="BUILD", help="Build UUID (or prefix) or id, as `ebs status` shows it."
)


@handle_errors
def gc_command(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be collected without deleting anything."
    ),
    domain: str | None = typer.Option(
        None, "--domain", metavar="D", help="Domain to collect; default: [gc].domain."
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Collect unused CAS objects and cache entries of one domain (TTL, quota, grace period)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    section = dict(services.config().get("gc", {}))
    configured = section.pop("domain", None)
    target = domain or configured
    if not isinstance(target, str) or not target:
        raise ConfigError("ebs gc collects one domain per run: give --domain D or set [gc].domain")
    config = GcConfig.from_mapping(section)
    report = collect(
        services.gc_store(), services.cas(target), clock=services.clock, config=config,
        dry_run=dry_run,
    )  # fmt: skip
    if json_mode:
        out.emit(report.model_dump(mode="json"))
    else:
        for line in render(report):
            out.line(line)


def _build_id(services: SiteServices, ref: str) -> BuildId:
    """A build recorded here, or any build of the metadata store by its numeric id."""
    try:
        return find_build(services.cwd, ref).build
    except ConfigError:
        if ref.isdigit():
            return BuildId(int(ref))
        raise


@handle_errors
def pin_command(
    ctx: typer.Context,
    build: str = BUILD_ARGUMENT,
    reason: str = typer.Option("", "--reason", help="Why the build is kept (shown to admins)."),
) -> None:
    """Keep a build's plan, outputs and logs from garbage collection."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=False, tty=services.is_tty())
    store = services.store(required=True)
    assert store is not None
    build_id = _build_id(services, build)
    store.pin_build(build_id, by=services.user(), reason=reason)
    out.line(f"build {build_id} pinned: GC keeps its plan, outputs and logs")


@handle_errors
def unpin_command(ctx: typer.Context, build: str = BUILD_ARGUMENT) -> None:
    """Remove a build's pin (its objects are then collected like any others)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=False, tty=services.is_tty())
    store = services.store(required=True)
    assert store is not None
    build_id = _build_id(services, build)
    if store.unpin_build(build_id):
        out.line(f"build {build_id} unpinned")
    else:
        out.line(f"build {build_id} was not pinned")
