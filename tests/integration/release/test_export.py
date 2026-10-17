"""Cross-domain export on PostgreSQL and two fs CAS domains (P2-02 R4)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest
from ebs.core.errors import ReleaseError
from ebs.meta.api import OutputResult
from ebs.meta.pg import PgMetadataStore
from ebs.release.export import export_release
from ebs.release.releases import create_release
from tests.helpers.pg import url_string
from tests.helpers.release import Action, finish


class Members:
    def __init__(self, *domains: str) -> None:
        self.domains = set(domains)

    def is_member(self, domain: str) -> bool:
        return domain in self.domains


@pytest.fixture
def pg(pg_url: URL) -> Iterator[PgMetadataStore]:
    store = PgMetadataStore.from_config({"url": url_string(pg_url)}, clock=FakeClock())
    try:
        yield store
    finally:
        store.close()


def _objects(cas: FsCAS, digest: Digest) -> list[Path]:
    """The files of a stored tree: its manifests and blobs."""
    paths = [cas.tree_path(digest)]
    for entry in cas.get_tree(digest).entries:
        if entry.digest is None:
            continue
        if entry.type == "dir":
            paths += _objects(cas, entry.digest)
        else:
            paths.append(cas.blob_path(entry.digest))
    return paths


# R4: blobs and trees are copied into the target domain's CAS (own inodes, never links), the
# release is created there with `exported_from`, an audit row is written, and the copies are
# registered and rooted in the target domain; without membership in both, nothing happens.
def test_copy_not_link_and_audit(tmp_path: Path, pg: PgMetadataStore) -> None:
    root = tmp_path / "cas"
    src, dst = FsCAS(root, "test"), FsCAS(root, "partner")
    tree_dir = tmp_path / "worklib"
    (tree_dir / "sub").mkdir(parents=True)
    (tree_dir / "a.v").write_text("module a; endmodule\n")
    (tree_dir / "sub" / "b.v").write_text("module b; endmodule\n")
    os.symlink("a.v", tree_dir / "link.v")
    tree = src.put_tree(tree_dir)
    report = src.put_bytes(b"lint report\n")
    outputs = {
        "worklib": OutputResult(digest=tree, id=tree, type="tree", size=40),  # file bytes below
        "report": OutputResult(digest=report, id=report, type="file", size=12),
    }
    build = finish(pg, {"gen": Action(outputs)})
    rel = create_release(
        pg, name="rtl-team/cpu", version="1.0", build=build,
        outputs=["gen.worklib", "gen.report"], by="alice",
    )  # fmt: skip
    dst.put_bytes(b"lint report\n")  # already in the target: kept as it is

    with pytest.raises(ReleaseError, match="not a member of 'partner'"):
        export_release(pg, rel, src=src, dst=dst, access=Members("test"), by="alice")
    with pytest.raises(ReleaseError, match="not a member of 'test'"):
        export_release(pg, rel, src=src, dst=dst, access=Members("partner"), by="alice")
    assert not dst.has(tree)
    assert pg.list_exports(rel.id) == []
    assert pg.list_releases("partner") == []

    exported = export_release(
        pg, rel, src=src, dst=dst, access=Members("test", "partner"), by="bob"
    )
    for path in [*_objects(src, tree), src.blob_path(report)]:
        copy = Path(str(path).replace(f"{os.sep}test{os.sep}", f"{os.sep}partner{os.sep}", 1))
        assert copy.is_file()
        assert not copy.is_symlink()
        assert copy.read_bytes() == path.read_bytes()
        assert copy.stat().st_ino != path.stat().st_ino  # a copy, not a hard link
    assert all(dst.verify(d) for d in (tree, report))
    assert exported.domain == "partner"
    assert exported.exported_from == rel.id
    assert exported.outputs == rel.outputs
    assert exported.created_by == "bob"
    [audit] = pg.list_exports(rel.id)
    assert (audit.release, audit.exported, audit.to_domain, audit.exported_by) == (
        rel.id, exported.id, "partner", "bob",
    )  # fmt: skip
    sizes = {b.digest: b.size for b in pg.gc_blobs("partner")}
    assert sizes[tree] == len("module a; endmodule\n") + len("module b; endmodule\n")
    assert sizes[report] == 12
    # Exporting again (a retry after a lost response) returns the export already made.
    again = export_release(pg, rel, src=src, dst=dst, access=Members("test", "partner"), by="bob")
    assert again == exported
    assert len(pg.list_exports(rel.id)) == 1
    registered = set(sizes)
    assert {tree, report} <= registered
    assert len(registered) == len(_objects(src, tree)) + 1
    assert pg.gc_roots("partner", FakeClock().now()).released == {tree, report}
    with pytest.raises(ReleaseError, match="already in domain"):
        export_release(pg, rel, src=src, dst=src, access=Members("test", "partner"), by="bob")
    pg.yank_release(rel.id, by="alice", reason="bad")
    yanked = pg.get_release_by_id(rel.id)
    assert yanked is not None
    with pytest.raises(ReleaseError, match="yanked"):
        export_release(pg, yanked, src=src, dst=FsCAS(root, "third"), access=Members(
            "test", "third"), by="bob")  # fmt: skip
