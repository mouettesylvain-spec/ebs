"""A sign-off bundle restored into an empty CAS and an empty PostgreSQL store verifies (P3-04 R4).

Site A (in-memory store) builds, releases and archives `verif/cpu 2026.10.1` with its outputs.
Site B has nothing: a migrated, empty database and an empty CAS. `ebs release import BUNDLE`
restores the plans, the released bytes, the build, its results and the release there, with an
archive record pointing at the bundle; `ebs verify NAME VERSION` then re-plans it and every action
key matches. Importing again changes nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.core.clock import FakeClock
from ebs.core.errors import ExitCode
from ebs.meta.pg import PgMetadataStore
from ebs.release.archive import archive_release
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url, url_string
from tests.helpers.signoff import hermetic_git, signed_off

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

pytestmark = pytest.mark.integration


# R4
def test_restore_into_empty_site_verifies(tmp_path: Path, pg_url: URL) -> None:
    a = signed_off(tmp_path / "a")
    record = archive_release(
        a.services.memory_store, a.cas, a.release, to=tmp_path / "cold", repo=a.site.proj,
        git=hermetic_git(), toolchains=a.services.toolchains(a.site.proj), by="alice",
        with_outputs=True,
    )  # fmt: skip
    bundle = record.location

    root = tmp_path / "b"
    write_site(root, config_extra=f'[metadata]\nurl = "{url_string(pg_url)}"\n')
    b = Site(root, TestServices(root, with_store=False))
    assert not (root / "cas").exists()
    pg = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        assert pg.get_release("test", "verif/cpu", "2026.10.1") is None

        imported = b.invoke(["release", "import", bundle, "--json"])
        assert imported.exit_code == ExitCode.OK, imported.output
        doc = json.loads(imported.stdout)
        assert (doc["name"], doc["version"], doc["outputs"]) == (
            "verif/cpu", "2026.10.1", a.release.model_dump(mode="json")["outputs"],
        )  # fmt: skip

        restored = pg.get_release("test", "verif/cpu", "2026.10.1")
        assert restored is not None
        build = pg.get_build(restored.build)
        original = a.services.memory_store.get_build(a.release.build)
        assert (build.uuid, build.plan_digest, build.final_plan_digest, build.status) == (
            original.uuid, original.plan_digest, original.final_plan_digest, original.status,
        )  # fmt: skip
        assert [r.action_id for r in pg.list_actions(build.id)] == [
            r.action_id for r in a.services.memory_store.list_actions(original.id)
        ]
        [archived] = pg.list_archives(restored.id)
        assert (archived.bundle, archived.location) == (record.bundle, bundle)

        cas = b.services.cas("test")
        assert original.final_plan_digest is not None
        assert cas.has(original.plan_digest)
        assert cas.has(original.final_plan_digest)
        for out in restored.outputs.values():
            assert cas.has(out.digest)

        verified = b.invoke(["verify", "verif/cpu", "2026.10.1", "--domain", "test", "--json"])
        assert verified.exit_code == ExitCode.OK, verified.output
        report = json.loads(verified.stdout)
        assert (report["ok"], report["actions"], report["matched"]) == (True, 3, 3)
        assert report["bundle"] == bundle

        again = b.invoke(["release", "import", bundle])
        assert again.exit_code == ExitCode.OK, again.output
        assert pg.list_archives(restored.id) == [archived]
        assert [r.id for r in pg.list_releases("test", name="verif/cpu")] == [restored.id]
    finally:
        pg.close()
