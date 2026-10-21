"""`ebs verify BUNDLE | NAME VERSION` (P3-04 R3): re-plan a release from its sign-off bundle and
check that every action key matches the recorded one, without executing anything.

With NAME VERSION the release's latest archive record names the bundle; its bytes must still have
the digest recorded when it was archived. Exit 0 when every key matches, 1 when one does not.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import handle_errors, make_output
from ebs.cli.plan import JSON_OPTION
from ebs.cli.release import DOMAIN_OPTION, default_domain, find_release, release_store
from ebs.core.digest import hash_file
from ebs.core.errors import ExitCode, ReleaseError
from ebs.release.archive import Git, open_bundle
from ebs.release.verify import verify_bundle

__all__ = ["verify_command"]


def _bundle_of(services: SiteServices, name: str, version: str, domain: str | None) -> Path:
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    archives = store.list_archives(rel.id)
    if not archives:
        raise ReleaseError(
            f"release {name} {version} has no sign-off bundle; run `ebs release archive {name} "
            f"{version} --to DIR` first, or give a bundle file"
        )
    latest = archives[-1]
    path = Path(latest.location)
    if not path.is_file():
        raise ReleaseError(
            f"the sign-off bundle of {name} {version} recorded at {path} is not readable; "
            "restore it from cold storage, or give a bundle file"
        )
    if hash_file(path)[0] != latest.bundle:
        raise ReleaseError(
            f"{path} does not match the digest recorded when {name} {version} was archived "
            f"({latest.bundle}); the file was altered or replaced"
        )
    return path


TARGET_ARGUMENT = typer.Argument(..., metavar="BUNDLE|NAME", help="Bundle file or release name.")
VERSION_ARGUMENT = typer.Argument(None, metavar="[VERSION]", help="Release version.")


@handle_errors
def verify_command(
    ctx: typer.Context,
    target: str = TARGET_ARGUMENT,
    version: str | None = VERSION_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Check that re-planning a release from its sign-off bundle gives the recorded action keys."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    if version is None:
        path = services.cwd / target
        if not path.is_file():
            raise ReleaseError(f"no bundle file {path}; give a bundle file, or NAME VERSION")
    else:
        path = _bundle_of(services, target, version, domain)
    with tempfile.TemporaryDirectory(prefix="ebs-verify-") as tmp:
        bundle = open_bundle(path, Path(tmp) / "bundle")
        report = verify_bundle(
            bundle, rules=services.rules(), git=Git(), work=Path(tmp) / "work",
            clock=services.clock,
        )  # fmt: skip
    if json_mode:
        out.emit({**report.to_json(), "bundle": str(path)})
    else:
        verdict = "verified" if report.ok else "MISMATCH"
        out.line(
            f"{verdict} {report.release} from {path}: {report.matched}/{report.actions} action "
            "keys match"
        )
        for diff in report.mismatches:
            fields = f": {', '.join(diff.changed)}" if diff.changed else ""
            out.line(f"  {diff.action_id}: {diff.status}{fields}")
        if report.unkeyed:
            out.line(f"  {len(report.unkeyed)} action(s) the build never keyed: not compared")
    if not report.ok:
        raise typer.Exit(int(ExitCode.ACTIONS_FAILED))
