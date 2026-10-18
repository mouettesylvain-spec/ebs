"""RTL release -> channel -> verification flow through `flow.lock` (P2-03 R6), end to end: the
`ebs` console script, PostgreSQL, the local executor and real `ebs-runner` processes.

The RTL flow packs its sources into a tree output, which is released and put on `stable`. The
verification flow imports `rtl-team/cpu` from `stable`; it cannot be built before `ebs lock
update` pins the release, then concatenates the imported sources. Moving `stable` to a new
release changes nothing for the verification build (fully cached, same bytes) until the lock is
updated; `ebs lock check` reports the pending change meanwhile.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest
from ebs.core.errors import MetadataError
from ebs.meta.api import BuildId, ReleaseId, ReleaseImport
from ebs.meta.pg import PgMetadataStore
from ebs.plan.planfile import decode
from ebs.release.lock import lock_digest, parse_lock
from tests.helpers.e2e import BuildRun, Site
from tests.helpers.imports import SRCS, publish
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string
from tests.helpers.release import finish

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

pytestmark = pytest.mark.integration

DOMAIN = "test"
RTL_FLOW = f"""\
version: 1
project: rtl
domain: {DOMAIN}
steps:
  pack:
    kind: shell
    script: "mkdir -p srcs && cp -R rtl srcs/"
    inputs: {{ rtl: "rtl/**" }}
    outputs: {{ srcs: {{ dir: srcs }} }}
"""
VERIF_FLOW = f"""\
version: 1
project: verif
domain: {DOMAIN}
imports:
  rtl: {{ from: rtl-team/cpu, channel: stable }}
steps:
  lint:
    kind: shell
    script: "cat imports/rtl/pack.srcs/rtl/*.sv > report.txt"
    inputs: {{ srcs: "${{imports.rtl}}/pack.srcs/**/*.sv" }}
    outputs: {{ report: report.txt }}
"""
CORE_V1 = "module core; endmodule\n"
CORE_V2 = "module core; wire w; endmodule\n"


@pytest.fixture
def site(tmp_path: Path, pg_url: URL) -> Site:
    return Site(tmp_path / "site", pg_url)


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


def _project(root: Path, flow: str) -> Path:
    root.mkdir(parents=True)
    (root / "flow.yaml").write_text(flow)
    return root


def _ok(site: Site, cwd: Path, *args: str) -> str:
    proc = site.ebs(cwd, *args)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _release(site: Site, rtl: Path, version: str) -> ReleaseId:
    """Build the RTL flow, release its sources as `version` and move `stable` to it."""
    built = site.build(rtl)
    build = str(built.result["build"])
    created = _ok(site, rtl, "release", "create", "rtl-team/cpu", version, "--build", build,
                  "--output", "pack.srcs", "--json")  # fmt: skip
    doc = json.loads(created)
    _ok(site, rtl, "channel", "set", "rtl-team/cpu", "stable", version, "--domain", DOMAIN)
    return ReleaseId(doc["id"])


def _report(site: Site, pg: PgMetadataStore, run: BuildRun) -> str:
    result = pg.get_result(BuildId(run.result["build"]), "lint")
    assert result is not None
    cas = FsCAS(site.root / "cas", DOMAIN)
    with cas.open(result.outputs["report"].digest) as f:
        return f.read().decode()


# R6
def test_rtl_release_channel_lock_build(tmp_path: Path, site: Site, pg: PgMetadataStore) -> None:
    rtl = _project(tmp_path / "rtl", RTL_FLOW)
    (rtl / "rtl").mkdir()
    (rtl / "rtl" / "core.sv").write_text(CORE_V1)
    verif = _project(tmp_path / "verif", VERIF_FLOW)
    first = _release(site, rtl, "1")

    unlocked = site.ebs(verif, "build", "--json")
    assert unlocked.returncode == 2, unlocked.stdout + unlocked.stderr
    assert "ebs lock update rtl" in unlocked.stderr
    assert "rtl: added rtl-team/cpu 1 (channel stable)" in _ok(site, verif, "lock", "update")
    _ok(site, verif, "lock", "check")

    one = site.build(verif)
    assert one.submitted == {"lint"}
    assert _report(site, pg, one) == CORE_V1
    rows = pg.list_release_imports(BuildId(one.result["build"]))
    assert rows == [ReleaseImport(alias="rtl", release=first)]

    # A new RTL release on `stable`: the verification build is unchanged until `lock update`.
    (rtl / "rtl" / "core.sv").write_text(CORE_V2)
    second = _release(site, rtl, "2")
    lock_text = (verif / "flow.lock").read_text()
    same = site.build(verif)
    assert same.submitted == set()
    assert same.cached == {"lint"}
    assert _report(site, pg, same) == CORE_V1
    assert pg.list_release_imports(BuildId(same.result["build"])) == rows
    check = site.ebs(verif, "lock", "check")
    assert check.returncode == 1, check.stdout + check.stderr
    assert "rtl: rtl-team/cpu 1 → 2 (channel stable); changed pack.srcs" in check.stdout
    assert (verif / "flow.lock").read_text() == lock_text

    assert "1 → 2" in _ok(site, verif, "lock", "update")
    _ok(site, verif, "lock", "check")
    two = site.build(verif)
    assert two.submitted == {"lint"}
    assert _report(site, pg, two) == CORE_V2
    assert pg.list_release_imports(BuildId(two.result["build"])) == [
        ReleaseImport(alias="rtl", release=second)
    ]
    # plan.json records the digest of the lock it was planned with (R1).
    digest = Digest.parse(json.loads(_ok(site, verif, "plan", "--json"))["plan"])
    with FsCAS(site.root / "cas", DOMAIN).open(digest) as f:
        plan = decode(f.read())
    assert plan.lock == lock_digest(parse_lock((verif / "flow.lock").read_text()))


# R5 on PostgreSQL: the store rules every backend shares, and the rows are append-only.
def test_release_imports_store_rules(tmp_path: Path, pg: PgMetadataStore) -> None:
    cas = FsCAS(tmp_path / "cas", DOMAIN)
    one = publish(pg, cas, tmp_path / "a", {"pack.srcs": SRCS})
    two = publish(pg, cas, tmp_path / "b", {"pack.srcs": SRCS}, version="2")
    other = publish(pg, FsCAS(tmp_path / "cas", "partner"), tmp_path / "c", {"pack.srcs": SRCS},
                    name="vip/x")  # fmt: skip
    build = finish(pg, {}, status=None)
    row = ReleaseImport(alias="rtl", release=one.id)
    pg.record_release_imports(build, [row, ReleaseImport(alias="alt", release=two.id)])
    pg.record_release_imports(build, [row])  # idempotent
    assert pg.list_release_imports(build) == [ReleaseImport(alias="alt", release=two.id), row]
    with pytest.raises(MetadataError, match="already imports"):
        pg.record_release_imports(build, [ReleaseImport(alias="rtl", release=two.id)])
    with pytest.raises(MetadataError, match="no release with id") as info:
        pg.record_release_imports(build, [ReleaseImport(alias="vip", release=other.id)])
    assert "partner" not in str(info.value)
    with pytest.raises(MetadataError, match="no release with id"):
        pg.record_release_imports(build, [ReleaseImport(alias="x", release=ReleaseId(999))])
    with pytest.raises(MetadataError, match="no build"):
        pg.list_release_imports(BuildId(999))
    assert len(pg.list_release_imports(build)) == 2  # nothing of the refused batches landed
