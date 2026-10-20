"""`ebs release create|show|list|yank|export` (P2-02).

`create NAME VERSION --build B --output step.output[selector]…` releases outputs of a finished
build (`ebs.release.releases`). `yank` withdraws a release without deleting it. `export --to D`
copies a release's objects into domain D's CAS and records it there (`ebs.release.export`).
`attest` writes the release's SLSA provenance as a DSSE envelope signed with the team key
(`[release.signing] key`); `verify-signature` checks one offline, with an allowed-signers file
and optionally the released blobs (P3-03).
Commands other than `create` work in `--domain`, by default the domain of the latest build
recorded in this directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from ebs.cli._context import SiteServices
from ebs.cli._output import Output, handle_errors, make_output
from ebs.cli._records import find_build
from ebs.cli.plan import JSON_OPTION
from ebs.core.errors import ConfigError, ReleaseError
from ebs.meta.api import BuildId, MetadataBackend, ReleaseView
from ebs.plan.planfile import decode
from ebs.release.attest import import_deps, provenance_statement
from ebs.release.export import UnixGroupAccess, export_release
from ebs.release.releases import create_release
from ebs.release.sign import SigningSettings, SshKeygen, SshSigner, sign_statement, verify_offline

__all__ = ["DOMAIN_OPTION", "app", "default_domain", "find_release", "release_store"]

app = typer.Typer(help="Releases: named, versioned selections of build outputs.",
                  no_args_is_help=True)  # fmt: skip

NAME_ARGUMENT = typer.Argument(..., metavar="NAME", help="Release name, e.g. rtl-team/cpu.")
VERSION_ARGUMENT = typer.Argument(..., metavar="VERSION", help="Release version, e.g. 2026.09.3.")
DOMAIN_OPTION = typer.Option(
    None, "--domain", metavar="D", help="Domain; default: that of the latest build here."
)

OUTPUT_OPTION = typer.Option(
    ...,
    "--output",
    metavar="STEP.OUTPUT[SEL]",
    help="Output to release: step.output, step.output[*] or step.output[col=value]; repeat.",
)


def release_store(services: SiteServices) -> MetadataBackend:
    store = services.store(required=True)
    assert store is not None  # required=True raises instead
    return store


def default_domain(services: SiteServices, domain: str | None) -> str:
    if domain is not None:
        return domain
    try:
        return find_build(services.cwd, None).domain
    except ConfigError:
        raise ConfigError(
            "give --domain D: there is no build recorded in this directory to take it from"
        ) from None


def find_release(store: MetadataBackend, domain: str, name: str, version: str) -> ReleaseView:
    found = store.get_release(domain, name, version)
    if found is None:
        raise ReleaseError(
            f"no release {name} {version} in domain {domain!r}; `ebs release list {name}` "
            "shows its versions"
        )
    return found


def _build_id(services: SiteServices, ref: str) -> BuildId:
    """A build recorded here (UUID prefix or id), or any build of the store by its id."""
    try:
        return find_build(services.cwd, ref).build
    except ConfigError:
        if ref.isdigit():
            return BuildId(int(ref))
        raise


def release_doc(r: ReleaseView) -> dict[str, Any]:
    return r.model_dump(mode="json")


def render_release(out: Output, r: ReleaseView) -> None:
    def row(label: str, value: object) -> None:
        out.line(f"{label:<12} {value}")

    row("release", f"{r.name} {r.version} (id {r.id}, domain {r.domain})")
    row("build", r.build)
    row("created", f"{r.created_at.isoformat()} by {r.created_by}")
    if r.exported_from is not None:
        row("exported", f"from release {r.exported_from}")
    if r.notes:
        row("notes", r.notes)
    if r.allow_failed is not None:
        row("failed ok", r.allow_failed)
    if r.yanked_at is not None:
        row("YANKED", f"{r.yanked_at.isoformat()} by {r.yanked_by}: {r.yank_reason}")
    row("outputs", len(r.outputs))
    width = max(len(n) for n in r.outputs)
    for name, o in sorted(r.outputs.items()):
        extra = "" if o.id == o.digest else f" (id {o.id})"
        out.line(f"  {name:<{width}}  {o.type:<4}  {o.digest}{extra}")


@app.command("create")
@handle_errors
def create_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    build: str = typer.Option(
        ..., "--build", metavar="BUILD", help="Build UUID (or prefix) or id, as `ebs status` shows."
    ),
    outputs: list[str] = OUTPUT_OPTION,
    notes: str = typer.Option("", "--notes", help="Release notes."),
    allow_failed: str | None = typer.Option(
        None,
        "--allow-failed",
        metavar="REASON",
        help="Also release outputs of actions that did not pass; the reason is stored.",
    ),
    json_mode: bool = JSON_OPTION,
) -> None:
    """Release selected outputs of a finished build as NAME VERSION."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    rel = create_release(
        release_store(services), name=name, version=version, build=_build_id(services, build),
        outputs=outputs, by=services.user(), notes=notes, allow_failed=allow_failed,
    )  # fmt: skip
    if json_mode:
        out.emit(release_doc(rel))
    else:
        out.line(f"released {rel.name} {rel.version} ({len(rel.outputs)} output(s))")
        render_release(out, rel)


@app.command("show")
@handle_errors
def show_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Show a release: its build, outputs, notes and yank state."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    if json_mode:
        out.emit(release_doc(rel))
    else:
        render_release(out, rel)


@app.command("list")
@handle_errors
def list_command(
    ctx: typer.Context,
    name: str | None = typer.Argument(None, metavar="[NAME]", help="Only this release name."),
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """List the releases of a domain (yanked ones included, marked)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    found = release_store(services).list_releases(default_domain(services, domain), name=name)
    if json_mode:
        out.emit({"v": 1, "releases": [release_doc(r) for r in found]})
        return
    if not found:
        out.line("no releases")
    for r in found:
        yanked = "  YANKED" if r.yanked_at is not None else ""
        out.line(
            f"{r.name} {r.version}  build {r.build}  {r.created_at.date()} {r.created_by}"
            f"  {len(r.outputs)} output(s){yanked}"
        )


@app.command("yank")
@handle_errors
def yank_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    reason: str = typer.Option(..., "--reason", help="Why the release is withdrawn (stored)."),
    domain: str | None = DOMAIN_OPTION,
) -> None:
    """Withdraw a release: it stays readable and reproducible, channels can no longer use it."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=False, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    yanked = store.yank_release(rel.id, by=services.user(), reason=reason)
    out.line(f"yanked {yanked.name} {yanked.version} ({yanked.yank_reason}); it is never deleted")


@app.command("export")
@handle_errors
def export_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    to: str = typer.Option(..., "--to", metavar="DOMAIN", help="Target domain."),
    domain: str | None = DOMAIN_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Copy a release into another domain (needs membership in both); audited."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    exported = export_release(
        store, rel, src=services.cas(rel.domain), dst=services.cas(to),
        access=UnixGroupAccess(services.runner_settings().cas_root_for), by=services.user(),
    )  # fmt: skip
    if json_mode:
        out.emit(release_doc(exported))
    else:
        out.line(
            f"exported {rel.name} {rel.version} to domain {to!r} (release {exported.id}, "
            f"{len(exported.outputs)} output(s) copied)"
        )


KEY_OPTION = typer.Option(
    None, "--key", metavar="FILE", help="Signing key; default: [release.signing] key."
)
OUT_OPTION = typer.Option(
    None,
    "--out",
    "-o",
    metavar="FILE",
    help="Where to write the envelope; default: ./<name>_<version>.intoto.json.",
)
ENVELOPE_ARGUMENT = typer.Argument(..., metavar="ENVELOPE", help="The .intoto.json file.")
ALLOWED_SIGNERS_OPTION = typer.Option(
    None,
    "--allowed-signers",
    metavar="FILE",
    help="Trusted keys (ssh-keygen ALLOWED SIGNERS); default: [release.signing].",
)
BLOB_OPTION = typer.Option(
    [],
    "--blob",
    metavar="PATH",
    help="A released file, tree directory or tree manifest to check; repeat.",
)


def _signing(services: SiteServices) -> SigningSettings:
    return SigningSettings.from_section(services.config().get("release", {}))


@app.command("attest")
@handle_errors
def attest_command(
    ctx: typer.Context,
    name: str = NAME_ARGUMENT,
    version: str = VERSION_ARGUMENT,
    domain: str | None = DOMAIN_OPTION,
    key: Path | None = KEY_OPTION,
    out_file: Path | None = OUT_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Write the release's signed SLSA provenance (a DSSE envelope of an in-toto statement)."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    store = release_store(services)
    rel = find_release(store, default_domain(services, domain), name, version)
    build = store.get_build(rel.build)
    with services.cas(rel.domain).open(build.final_plan_digest or build.plan_digest) as f:
        plan = decode(f.read(), any_key_schema=True)
    statement = provenance_statement(rel, build, plan, import_deps(store, build.id))
    conf = _signing(services)
    signer = SshSigner(SshKeygen(conf.ssh_keygen), services.cwd / conf.require_key(key))
    envelope = sign_statement(statement, signer)
    default = Path(f"{rel.name.replace('/', '_')}_{rel.version}.intoto.json")
    target = services.cwd / (out_file or default)
    target.write_text(envelope.to_json())
    keyid = envelope.signatures[0].keyid
    if json_mode:
        out.emit({"v": 1, "envelope": str(target), "subjects": sorted(rel.outputs), "keyid": keyid})
    else:
        out.line(
            f"attested {rel.name} {rel.version}: {len(rel.outputs)} subject(s), signed by "
            f"{keyid or signer.key} -> {target}"
        )


def _attested_release(statement: dict[str, Any]) -> str:
    try:
        rel = statement["predicate"]["buildDefinition"]["externalParameters"]["release"]
        return f"{rel['name']} {rel['version']}"
    except (KeyError, TypeError):
        return "(no release named)"


@app.command("verify-signature")
@handle_errors
def verify_signature_command(
    ctx: typer.Context,
    envelope: Path = ENVELOPE_ARGUMENT,
    allowed_signers: Path | None = ALLOWED_SIGNERS_OPTION,
    blobs: list[Path] = BLOB_OPTION,
    json_mode: bool = JSON_OPTION,
) -> None:
    """Verify a release attestation offline: its signature, and the blobs against its subjects."""
    services: SiteServices = ctx.obj
    out = make_output(services.environ, json_mode=json_mode, tty=services.is_tty())
    conf = _signing(services)
    cwd = services.cwd
    verified = verify_offline(
        cwd / envelope, cwd / conf.require_allowed_signers(allowed_signers),
        [cwd / b for b in blobs], keygen=SshKeygen(conf.ssh_keygen),
    )  # fmt: skip
    label = _attested_release(verified.statement)
    if json_mode:
        out.emit({"v": 1, "verified": True, "release": label,
                  "principals": list(verified.principals), "statement": verified.statement,
                  "blobs": {n: str(p) for n, p in sorted(verified.subjects.items())},
                  "manifest_only": list(verified.manifest_only)})  # fmt: skip
        return
    out.line(f"verified {label}: signed by {', '.join(verified.principals)}")
    for subject, path in sorted(verified.subjects.items()):
        note = (
            "  (tree manifest only: its files were not checked; pass the directory)"
            if subject in verified.manifest_only
            else ""
        )
        out.line(f"  {subject}  matches {path}{note}")
    if not blobs:
        out.line("  (no --blob given: the released bytes were not checked)")
