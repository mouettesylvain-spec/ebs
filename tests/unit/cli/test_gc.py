"""`ebs gc`, `ebs pin`, `ebs unpin` (P1-09 R2, R6)."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from ebs.core.errors import ExitCode
from ebs.meta.api import BlobRef, BuildCreate, BuildId
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.outputs import U1, write_outputs_build


# R6: `ebs gc --dry-run --json` prints the report and records the run; nothing is deleted.
def test_gc_dry_run_json(tmp_path: Path) -> None:
    write_site(tmp_path, config_extra=GC_DOMAINS)
    site = Site(tmp_path, TestServices(tmp_path))
    cas = site.services.cas("test")
    blob = cas.put_bytes(b"old object")
    store = site.services.memory_store
    store.register_blobs("test", [BlobRef(digest=blob, size=10, kind="file")])
    site.services.clock.sleep(40 * 86_400)
    result = site.invoke(["gc", "--domain", "test", "--dry-run", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    report = json.loads(result.stdout)
    assert report["dry_run"] is True
    assert report["domain"] == "test"
    assert report["tombstoned"] == 1
    assert report["reclaimable"]["bytes"] == 10
    assert cas.has(blob)
    assert store.gc_tombstones("test") == []
    assert [r.dry_run for r in store.gc_runs] == [True]

    text = site.invoke(["gc", "--domain", "test"])
    assert text.exit_code == ExitCode.OK, text.output
    assert "gc test at" in text.stdout
    assert "tombstoned: 1" in text.stdout
    assert [t.digest for t in store.gc_tombstones("test")] == [blob]


GC_DOMAINS = '[gc]\ndomains = ["test"]\n'  # P2-01 R5: the GC account's domains


# One domain per run: from --domain or [gc].domain; [gc] settings are validated.
def test_gc_domain_and_config(tmp_path: Path) -> None:
    write_site(tmp_path, config_extra='[gc]\ndomain = "test"\ngrace_hours = 2\n')
    site = Site(tmp_path, TestServices(tmp_path))
    assert site.invoke(["gc"]).exit_code == ExitCode.OK
    bare = tmp_path / "bare"
    write_site(bare)
    plain = Site(bare, TestServices(bare))
    missing = plain.invoke(["gc"])
    assert missing.exit_code == ExitCode.USAGE
    assert "--domain" in missing.output
    bad = tmp_path / "bad"
    write_site(bad, config_extra='[gc]\ndomains = ["test"]\ngrace_hours = 0\n')
    wrong = Site(bad, TestServices(bad)).invoke(["gc", "--domain", "test"])
    assert wrong.exit_code == ExitCode.USAGE
    assert "grace_hours" in wrong.output


# R7: GC needs direct database access as the service account, never the HTTP service.
def test_gc_needs_postgres(tmp_path: Path) -> None:
    write_site(
        tmp_path,
        config_extra='[metadata]\nurl = "https://meta.example.invalid"\n' + GC_DOMAINS,
    )
    site = Site(tmp_path, TestServices(tmp_path, with_store=False))
    result = site.invoke(["gc", "--domain", "test"])
    assert result.exit_code == ExitCode.USAGE
    assert "service account" in result.output


# P2-01 R5: GC refuses a domain its service account is not explicitly listed for.
def test_gc_listed_domains_only(tmp_path: Path) -> None:
    write_site(tmp_path, config_extra='[gc]\ndomains = ["test", "cpu-nda"]\n')
    site = Site(tmp_path, TestServices(tmp_path))
    assert site.invoke(["gc", "--domain", "test", "--dry-run"]).exit_code == ExitCode.OK
    refused = site.invoke(["gc", "--domain", "partner", "--dry-run"])
    assert refused.exit_code == ExitCode.USAGE
    assert "not listed for this GC account" in refused.output
    bare = tmp_path / "bare"
    write_site(bare)
    unlisted = Site(bare, TestServices(bare)).invoke(["gc", "--domain", "test", "--dry-run"])
    assert unlisted.exit_code == ExitCode.USAGE
    assert "[gc].domains" in unlisted.output
    bad = tmp_path / "bad"
    write_site(bad, config_extra='[gc]\ndomains = "test"\n')
    wrong = Site(bad, TestServices(bad)).invoke(["gc", "--domain", "test"])
    assert wrong.exit_code == ExitCode.USAGE
    assert "must be a list" in wrong.output


# R2: pinning a build makes its outputs GC roots; unpinning removes them.
def test_pin_unpin(site: Site) -> None:
    built = write_outputs_build(site)
    store = site.services.memory_store
    now = site.services.clock.now()
    report = built.results["gen"].outputs["report"].digest

    pinned = site.invoke(["pin", str(U1)[:8], "--reason", "tape-out"])
    assert pinned.exit_code == ExitCode.OK, pinned.output
    assert f"build {built.build} pinned" in pinned.stdout
    assert store.get_build(BuildId(built.build)).pinned is True
    assert report in store.gc_roots("test", now).pinned

    assert site.invoke(["unpin", str(built.build)]).stdout.strip().endswith("unpinned")
    assert "was not pinned" in site.invoke(["unpin", str(built.build)]).stdout
    assert report not in store.gc_roots("test", now).pinned
    # A build recorded elsewhere (CI) is pinned by its id.
    view = store.get_build(BuildId(built.build))
    spec = BuildCreate(
        **{k: getattr(view, k) for k in BuildCreate.model_fields} | {"uuid": uuid4()}
    )
    other = store.create_build(spec)
    assert site.invoke(["pin", str(other)]).exit_code == ExitCode.OK
    assert store.get_build(other).pinned is True
    unknown = site.invoke(["pin", "nope"])
    assert unknown.exit_code == ExitCode.USAGE
