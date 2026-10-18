"""`ebs domain list|check`: the configured confidentiality domains (P2-01 R4).

`list` shows each `[[domains]]` entry with its group, roots, TTL, quota and whether you are a
member. `check` verifies what deploy/admin/create_domain.sh sets up: the CAS directory
(`<cas root>/<name>`, group `unix_group`, 2750), its `blobs`, `trees` and `tmp` (2770), the debug
directory (2770) and free space on the CAS filesystem; it exits 3 if anything is wrong.
"""

from __future__ import annotations

from typing import Any

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.core.errors import ConfigError, ExitCode
from ebs.domains import DomainCheck, check_domain

__all__ = ["app"]

app = typer.Typer(help="Confidentiality domains ([[domains]] in ebs.toml).", no_args_is_help=True)


def _size(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("P", "T", "G", "M", "K"):
        scale = 1 << {"K": 10, "M": 20, "G": 30, "T": 40, "P": 50}[unit]
        if n >= scale:
            return f"{n / scale:.1f}{unit}"
    return f"{n}B"


@app.command("list")
@handle_errors
def list_command(ctx: typer.Context, json_mode: bool = JSON_OPTION) -> None:
    """The configured domains, and whether you are a member of each."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    domains = services.domains()
    settings = services.runner_settings()
    groups = services.user_groups()
    docs: list[dict[str, Any]] = [
        {
            "name": d.name,
            "unix_group": d.unix_group,
            "member": d.unix_group in groups,
            "cas": str(settings.cas_root_for(d.name) / d.name),
            "debug": None
            if (root := settings.debug_root_for(d.name)) is None
            else str(root / d.name),
            "ttl_days": d.ttl_days,
            "quota_bytes": d.quota_bytes,
        }
        for d in domains.by_name.values()
    ]
    if json_mode:
        out.emit({"v": 1, "domains": docs})
        return
    if not docs:
        out.line("no domains configured ([[domains]] in ebs.toml): the site is unrestricted")
    for d in docs:
        member = "member" if d["member"] else "not a member"
        out.line(
            f"{d['name']}  group {d['unix_group']} ({member})  cas {d['cas']}  "
            f"debug {d['debug'] or '-'}  ttl {d['ttl_days']}d  quota {_size(d['quota_bytes'])}"
        )


def _doc(c: DomainCheck) -> dict[str, Any]:
    return {
        "domain": c.domain,
        "ok": c.ok,
        "free_bytes": c.free_bytes,
        "total_bytes": c.total_bytes,
        "problems": list(c.problems),
        "dirs": [{"path": str(d.path), "problems": list(d.problems)} for d in c.dirs],
    }


@app.command("check")
@handle_errors
def check_command(
    ctx: typer.Context,
    min_free: float = typer.Option(
        10.0, "--min-free", min=0.0, max=100.0, help="Minimum free space on the CAS, in percent."
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Check modes, groups and free space of every configured domain (exit 3 on a problem)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    domains = services.domains()
    if not domains.configured:
        raise ConfigError("no domains configured: add [[domains]] entries to ebs.toml")
    settings = services.runner_settings()
    checks = [
        check_domain(
            d,
            cas_root=settings.cas_root_for(d.name),
            debug_root=settings.debug_root_for(d.name),
            group_id=services.group_id,
            disk_usage=services.disk_usage,
            min_free_ratio=min_free / 100,
        )
        for d in domains.by_name.values()
    ]
    if json_mode:
        out.emit({"v": 1, "ok": all(c.ok for c in checks), "domains": [_doc(c) for c in checks]})
    else:
        for c in checks:
            free = (
                ""
                if c.free_bytes is None or not c.total_bytes
                else f"  {_size(c.free_bytes)} free of {_size(c.total_bytes)}"
            )
            out.line(f"{c.domain}: {'ok' if c.ok else 'PROBLEMS'}{free}")
            for d in c.dirs:
                for problem in d.problems:
                    out.line(f"  {d.path}: {problem}")
            for problem in c.problems:
                out.line(f"  {problem}")
    if not all(c.ok for c in checks):
        raise typer.Exit(ExitCode.INFRA)
