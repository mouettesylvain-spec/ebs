"""`ebs used-by NAME VERSION`: every build and release downstream of a release (P2-04 R2).

Transitive: through releases made from downstream outputs and through exports into other
domains (when the caller may read them; otherwise they are listed as hidden). The release's own
build is not listed.
"""

from __future__ import annotations

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.cli.release import (
    DOMAIN_OPTION,
    NAME_ARGUMENT,
    VERSION_ARGUMENT,
    default_domain,
    find_release,
    release_store,
)
from ebs.cli.why import DEPTH_OPTION
from ebs.release.provenance import used_by

__all__ = ["used_by_command"]


def _table(out: Output, rows: list[tuple[str, ...]]) -> None:
    if not rows:
        return
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        out.line("  " + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())


@handle_errors
def used_by_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    depth: int = DEPTH_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """List the builds and releases that consumed a release, transitively."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    found = used_by(store, rel, depth=depth)
    if json_mode:
        out.emit({
            "v": 1,
            "release": {"domain": rel.domain, "id": rel.id, "name": rel.name,
                        "version": rel.version},
            "truncated": found.truncated,
            "builds": [
                {"domain": b.domain, "build": b.build, "project": b.project,
                 "flow_repo": b.flow_repo, "flow_commit": b.flow_commit, "user": b.user_name,
                 "created_at": b.created_at.isoformat()}
                for b in found.builds
            ],
            "releases": [
                {"domain": r.domain, "id": r.release, "name": r.name, "version": r.version,
                 "via": r.via}
                for r in found.releases
            ],
            "hidden": [
                {"domain": h.domain, "release": h.release, "reason": h.reason}
                for h in found.hidden
            ],
        })  # fmt: skip
        return
    out.line(f"used by {rel.name} {rel.version} (domain {rel.domain})")
    out.line(f"builds: {len(found.builds)}")
    _table(out, [
        (b.domain, f"build {b.build}",
         b.project + (f"@{b.flow_commit[:12]}" if b.flow_commit else ""), b.user_name,
         b.created_at.date().isoformat())
        for b in found.builds
    ])  # fmt: skip
    out.line(f"releases: {len(found.releases)}")
    _table(out, [
        (r.domain, f"{r.name} {r.version}", f"release {r.release}",
         "export" if r.via == "export" else "from its outputs")
        for r in found.releases
    ])  # fmt: skip
    for h in found.hidden:
        out.line(f"(hidden: {h.reason})")
    if found.truncated:
        out.line(f"(truncated at depth {depth}: raise --depth to see further)")
