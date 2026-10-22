"""`restore_bundle` (`ebs release import`, P3-04 R4) on the in-memory store; the PostgreSQL run
end to end is tests/integration/test_archive_restore.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import MetadataError
from ebs.meta.api import ReleaseCreate, ReleaseView
from ebs.meta.memory import InMemoryMetadataStore
from ebs.release.archive import Bundle, archive_release, open_bundle, restore_bundle
from ebs.release.verify import verify_bundle
from tests.helpers.signoff import SignedOff, hermetic_git, signed_off


def _bundle(s: SignedOff, tmp_path: Path, *, with_outputs: bool) -> Bundle:
    rec = archive_release(
        s.services.memory_store, s.cas, s.release, to=tmp_path / "cold", repo=s.site.proj,
        git=hermetic_git(), toolchains=s.services.toolchains(s.site.proj), by="alice",
        with_outputs=with_outputs,
    )  # fmt: skip
    return open_bundle(Path(rec.location), tmp_path / "open")


# R4: a bundle without the released bytes restores the records and plans; the outputs are not
# in the new CAS, and the release still verifies.
def test_lean_bundle(tmp_path: Path) -> None:
    s = signed_off(tmp_path / "a")
    bundle = _bundle(s, tmp_path, with_outputs=False)
    store = InMemoryMetadataStore(clock=FakeClock())
    cas = FsCAS(tmp_path / "b-cas", "test")
    rel = restore_bundle(bundle, store, cas, location=str(bundle.path), by="carol")
    assert rel.outputs == s.release.outputs
    build = store.get_build(rel.build)
    assert build.final_plan_digest is not None
    assert cas.has(build.plan_digest)
    assert cas.has(build.final_plan_digest)
    assert not any(cas.has(o.digest) for o in rel.outputs.values())
    [archived] = store.list_archives(rel.id)
    assert (archived.bundle, archived.created_by) == (bundle.digest, "carol")
    report = verify_bundle(bundle, rules=s.services.rules(), git=hermetic_git(),
                           work=tmp_path / "w")  # fmt: skip
    assert report.ok


class _FailOnce(InMemoryMetadataStore):
    def __init__(self) -> None:
        super().__init__(clock=FakeClock())
        self.fail = True

    def create_release(self, spec: ReleaseCreate, *, by: str) -> ReleaseView:
        if self.fail:
            self.fail = False
            raise MetadataError("connection lost")
        return super().create_release(spec, by=by)


# R4: an import that stopped after recording the build can be run again.
def test_retry_after_failure(tmp_path: Path) -> None:
    s = signed_off(tmp_path / "a")
    bundle = _bundle(s, tmp_path, with_outputs=True)
    store = _FailOnce()
    cas = FsCAS(tmp_path / "b-cas", "test")
    with pytest.raises(MetadataError, match="connection lost"):
        restore_bundle(bundle, store, cas, location=str(bundle.path), by="carol")
    rel = restore_bundle(bundle, store, cas, location=str(bundle.path), by="carol")
    assert rel.outputs == s.release.outputs
    assert store.get_build(rel.build).uuid != bundle.build().uuid  # the first one is taken
    assert restore_bundle(bundle, store, cas, location=str(bundle.path), by="carol") == rel
    assert len(store.list_archives(rel.id)) == 1
