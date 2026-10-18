"""`ebs tests NAME VERSION`: every simulation run on (what derives from) a release (P2-04 R3).

Lists each `*.sim` action whose inputs derive from the release's outputs, in its domain and in
the domains it was exported to: test, seed, status, toolchain, build and date. `--latest` keeps
the newest run per (test, seed); `--junit FILE` also writes the runs as JUnit XML.
"""

from __future__ import annotations

from pathlib import Path

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
from ebs.cli.why import DEPTH_OPTION, short
from ebs.core.errors import ConfigError
from ebs.release.provenance import SimRuns, TestRun, junit_xml, latest, sim_runs

__all__ = ["list_tests_command"]

JUNIT_OPTION = typer.Option(
    None, "--junit", metavar="FILE", help="Also write the runs as JUnit XML to FILE."
)


def _row(r: TestRun) -> dict[str, object]:
    return {
        "domain": r.domain, "build": r.build, "action_id": r.action_id, "test": r.test,
        "seed": r.seed, "status": r.status, "state": r.state, "cached": r.cached,
        "toolchain": None if r.toolchain is None else str(r.toolchain),
        "finished_at": None if r.finished_at is None else r.finished_at.isoformat(),
        "project": r.project, "rule": r.rule,
    }  # fmt: skip


def _notes(out: Output, found: SimRuns, depth: int) -> None:
    for h in found.hidden:
        out.line(f"(hidden: {h.reason})")
    if found.truncated:
        out.line(f"(truncated at depth {depth}: raise --depth to see further)")


def _text(value: object) -> str:
    return "-" if value is None else str(value)


@handle_errors
def list_tests_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    newest: bool = typer.Option(False, "--latest", help="Keep the newest run per (test, seed)."),
    junit: Path | None = JUNIT_OPTION,
    depth: int = DEPTH_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """List the simulations that ran on a release (or on what was built from it)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    found = sim_runs(store, rel, depth=depth)
    runs = latest(found.runs) if newest else list(found.runs)
    if junit is not None:
        try:
            junit.parent.mkdir(parents=True, exist_ok=True)
            junit.write_text(junit_xml(runs, suite=f"{rel.name} {rel.version}"), encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot write the JUnit report {junit}: {exc}") from exc
    if json_mode:
        out.emit({
            "v": 1,
            "release": {"domain": rel.domain, "id": rel.id, "name": rel.name,
                        "version": rel.version},
            "tests": [_row(r) for r in runs],
            "truncated": found.truncated,
            "hidden": [
                {"domain": h.domain, "release": h.release, "reason": h.reason}
                for h in found.hidden
            ],
        })  # fmt: skip
        return
    failed = sum(r.status == "failed" for r in runs)
    out.line(
        f"tests on {rel.name} {rel.version} (domain {rel.domain}): {len(runs)} run(s), "
        f"{failed} failed"
    )
    _notes(out, found, depth)
    if not runs:
        return
    header = ("domain", "build", "test", "seed", "status", "toolchain", "finished", "action")
    rows = [
        (
            r.domain, str(r.build), _text(r.test), _text(r.seed),
            _text(r.status) + (" (cached)" if r.cached else ""),
            "-" if r.toolchain is None else short(r.toolchain),
            "-" if r.finished_at is None else r.finished_at.isoformat(timespec="seconds"),
            r.action_id,
        )
        for r in runs
    ]  # fmt: skip
    widths = [max(len(row[i]) for row in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        out.line("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    if junit is not None:
        out.line(f"wrote {junit}")
