"""Export details without PostgreSQL (P2-02 R4); the full test is tests/integration/release."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import CasError, ReleaseError
from ebs.meta.api import OutputResult
from ebs.meta.memory import InMemoryMetadataStore
from ebs.release.export import UnixGroupAccess, export_release
from ebs.release.releases import create_release
from tests.helpers.release import Action, finish


# R4: membership is the Unix group (or owner) of the domain's CAS directory.
def test_unix_group_access(tmp_path: Path) -> None:
    (tmp_path / "test").mkdir()
    gid = (tmp_path / "test").stat().st_gid
    owner = UnixGroupAccess(tmp_path, uid=os.getuid, groups=lambda: [])
    in_group = UnixGroupAccess(tmp_path, uid=lambda: -1, groups=lambda: [gid])
    outsider = UnixGroupAccess(tmp_path, uid=lambda: -1, groups=lambda: [gid + 1])
    assert owner.is_member("test")
    assert in_group.is_member("test")
    assert not outsider.is_member("test")
    assert not owner.is_member("missing")  # no CAS directory: no domain to belong to


class Everyone:
    def is_member(self, domain: str) -> bool:
        return True


# R4: an object missing from the source CAS fails the export before anything is recorded.
def test_missing_object(tmp_path: Path) -> None:
    store = InMemoryMetadataStore(clock=FakeClock())
    src, dst = FsCAS(tmp_path, "test"), FsCAS(tmp_path, "partner")
    blob = src.put_bytes(b"x")
    build = finish(store, {"gen": Action({"o": OutputResult(digest=blob, id=blob, type="file",
                                                            size=1)})})  # fmt: skip
    rel = create_release(store, name="cpu", version="1", build=build, outputs=["gen.o"], by="alice")
    src.delete(blob)
    with pytest.raises(CasError, match=r"cannot copy output gen\.o"):
        export_release(store, rel, src=src, dst=dst, access=Everyone(), by="alice")
    assert store.list_releases("partner") == []
    with pytest.raises(ReleaseError, match="source CAS is domain 'partner'"):
        export_release(store, rel, src=dst, dst=src, access=Everyone(), by="alice")
