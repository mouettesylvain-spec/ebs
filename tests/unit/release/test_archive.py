"""Sign-off bundles: `ebs release archive` (P3-04 R1, R2)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
import zstandard

from ebs.cli._records import find_build
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import ExitCode, ReleaseError
from ebs.meta.api import ArchiveRecord, BuildView, ReleaseView
from ebs.plan.planfile import encode
from ebs.release.archive import (
    MANIFEST,
    archive_release,
    bundle_file_name,
    open_bundle,
    write_archive,
    write_bundle,
)
from ebs.release.lock import release_digest
from ebs.release.releases import create_release
from tests.helpers.attest import sample_plan
from tests.helpers.signoff import FLOW, SignedOff, hermetic_git, signed_off


def _archive(s: SignedOff, to: Path, *, with_outputs: bool = False) -> ArchiveRecord:
    return archive_release(
        s.services.memory_store, s.cas, s.release, to=to, repo=s.site.proj, git=hermetic_git(),
        toolchains=s.services.toolchains(s.site.proj), by="alice", with_outputs=with_outputs,
    )  # fmt: skip


def _tar(path: Path) -> list[tarfile.TarInfo]:
    data = zstandard.ZstdDecompressor().stream_reader(path.read_bytes()).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        return tar.getmembers()


# R1: archiving the same release twice gives the same bytes: sorted entries, fixed mtimes,
# owners and modes, whatever order the members were collected in.
def test_deterministic_bytes(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    a = _archive(s, tmp_path / "cold-a")
    b = _archive(s, tmp_path / "cold-b", with_outputs=False)
    assert Path(a.location).read_bytes() == Path(b.location).read_bytes()
    assert a.bundle == b.bundle == hash_file(Path(a.location))[0]

    infos = _tar(Path(a.location))
    names = [i.name for i in infos]
    assert names[0] == MANIFEST
    assert names[1:] == sorted(names[1:])
    assert {(i.mtime, i.uid, i.gid, i.uname, i.gname, i.mode) for i in infos} == {
        (0, 0, 0, "", "", 0o644)
    }
    assert all(i.isfile() for i in infos)

    members: dict[str, bytes | Path] = {"b/2.json": b"2", "a/1.json": b"1", "c": tmp_path / "c"}
    (tmp_path / "c").write_bytes(b"3")
    first = write_bundle(members, tmp_path / "x.tar.zst", header={"format": "t"})
    again = write_bundle(dict(reversed(members.items())), tmp_path / "y.tar.zst",
                         header={"format": "t"})  # fmt: skip
    assert first == again
    assert (tmp_path / "x.tar.zst").read_bytes() == (tmp_path / "y.tar.zst").read_bytes()


# R1: the bundle holds the release and its attestation, the plan, a git bundle of the flow repo
# at the recorded commit, the lock closure, the toolchain records, the trees the imports need,
# optionally the released bytes, and a manifest with the digest of every other member.
def test_members_manifest(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    rec = _archive(s, tmp_path / "cold", with_outputs=True)
    bundle = open_bundle(Path(rec.location), tmp_path / "open")
    assert bundle.digest == rec.bundle
    members = bundle.members()

    manifest = json.loads((bundle.root / MANIFEST).read_bytes())
    assert set(manifest["members"]) == set(members)
    for name, path in members.items():
        data = path.read_bytes()
        assert manifest["members"][name] == {"digest": str(hash_bytes(data)), "size": len(data)}

    assert ReleaseView.model_validate_json(members["release.json"].read_bytes()) == s.release
    build = BuildView.model_validate_json(members["build.json"].read_bytes())
    assert build == store.get_build(s.release.build)
    statement = json.loads(members["attestation.json"].read_bytes())
    assert [x["name"] for x in statement["subject"]] == sorted(s.release.outputs)

    assert build.final_plan_digest is not None
    assert hash_bytes(members["plan.json"].read_bytes()) == build.plan_digest
    assert hash_bytes(members["final-plan.json"].read_bytes()) == build.final_plan_digest
    plan = bundle.plan()
    assert plan.flow.git is not None
    assert manifest["flow"] == {
        "path": plan.flow.path, "repo": plan.flow.git.repo, "commit": plan.flow.git.commit
    }  # fmt: skip

    clone = tmp_path / "clone"
    hermetic_git().checkout(members["git/flow.bundle"], plan.flow.git.commit, clone)
    assert (clone / "flow.yaml").read_text() == FLOW
    assert (clone / "flow.lock").read_text() == (s.site.proj / "flow.lock").read_text()

    up = f"imports/{release_digest(s.upstream).hex}"
    assert ReleaseView.model_validate_json(members[f"{up}/release.json"].read_bytes()) == (
        s.upstream
    )
    assert members[f"{up}/plan.json"].read_bytes() == encode(sample_plan())
    # The closure is recursive: the upstream's own import is bundled as well.
    base = store.get_release("test", "lib/base", "1")
    assert base is not None
    deep = f"imports/{release_digest(base).hex}"
    assert ReleaseView.model_validate_json(members[f"{deep}/release.json"].read_bytes()) == base
    assert members[f"{deep}/plan.json"].read_bytes() == encode(sample_plan())
    assert manifest["imports"] == sorted(
        [{"name": "rtl-team/cpu", "version": "1", "release": str(release_digest(s.upstream)),
          "dir": up},
         {"name": "lib/base", "version": "1", "release": str(release_digest(base)),
          "dir": deep}],
        key=lambda i: i["dir"],
    )  # fmt: skip
    # Only the direct imports' trees are needed to re-plan.
    lib = base.outputs["gen.lib"].digest
    assert f"cas/trees/{lib.algo}/{lib.hex}" not in members

    tc = json.loads(members["toolchains/fake.json"].read_bytes())
    assert tc["module"] == "fakesim/1.0"
    assert tc["env"] == {"FAKESIM_MODE": "batch"}
    assert tc["id"] == str(plan.toolchains["fake"].id)

    srcs = s.upstream.outputs["pack.srcs"].digest
    tree = s.cas.get_tree(srcs)
    assert (
        members[f"cas/trees/{srcs.algo}/{srcs.hex}"].read_bytes()
        == s.cas.tree_path(srcs).read_bytes()
    )
    sub = next(e for e in tree.entries if e.name == "rtl").digest
    assert sub is not None
    assert f"cas/trees/{sub.algo}/{sub.hex}" in members
    for out in s.release.outputs.values():
        blob = members[f"cas/blobs/{out.digest.algo}/{out.digest.hex}"]
        assert blob.read_bytes() == f"{out.action_id}/{out.output}".encode()

    # Without --with-outputs the released bytes stay out; the trees the imports need do not.
    lean = open_bundle(Path(_archive(s, tmp_path / "lean").location), tmp_path / "open-lean")
    assert not any(n.startswith("cas/blobs/") for n in lean.members())
    assert f"cas/trees/{srcs.algo}/{srcs.hex}" in lean.members()


# R1: a member that does not match the manifest, or one it does not list, is refused.
def test_tampered_bundle_refused(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    good = open_bundle(Path(_archive(s, tmp_path / "cold").location), tmp_path / "open")
    entries: dict[str, bytes | Path] = {MANIFEST: good.root / MANIFEST, **good.members()}
    edited = {**entries, "release.json": b"{}"}
    write_archive(edited, tmp_path / "edited.tar.zst")
    with pytest.raises(ReleaseError, match=r"release\.json.*does not match"):
        open_bundle(tmp_path / "edited.tar.zst", tmp_path / "x1")
    write_archive({**entries, "extra.txt": b"x"}, tmp_path / "extra.tar.zst")
    with pytest.raises(ReleaseError, match=r"extra\.txt.*not listed"):
        open_bundle(tmp_path / "extra.tar.zst", tmp_path / "x2")
    missing = dict(entries)
    del missing["build.json"]
    write_archive(missing, tmp_path / "missing.tar.zst")
    with pytest.raises(ReleaseError, match=r"build\.json.*missing"):
        open_bundle(tmp_path / "missing.tar.zst", tmp_path / "x3")


# R2: the bundle is written to the cold storage directory and recorded in `archives`; the same
# bytes again record nothing new, other bytes never overwrite an archived file.
def test_records_location(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    store = s.services.memory_store
    cold = tmp_path / "cold"
    rec = _archive(s, cold)
    path = cold / bundle_file_name(s.release)
    assert rec.location == str(path)
    assert path.is_file()
    assert not [p for p in cold.iterdir() if p != path]  # no temporary file left behind
    assert (rec.release, rec.bundle, rec.created_by) == (s.release.id, hash_file(path)[0], "alice")
    assert store.list_archives(s.release.id) == [rec]

    assert _archive(s, cold) == rec
    assert store.list_archives(s.release.id) == [rec]
    with pytest.raises(ReleaseError, match="already holds"):
        _archive(s, cold, with_outputs=True)
    assert hash_file(path)[0] == rec.bundle


# R2 through the CLI: `ebs release archive --to DIR` from the flow's repository.
def test_archive_cli(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    args = ["release", "archive", "verif/cpu", "2026.10.1", "--domain", "test"]
    done = s.site.invoke([*args, "--to", str(tmp_path / "cold"), "--json"])
    assert done.exit_code == ExitCode.OK, done.output
    doc = json.loads(done.stdout)
    [rec] = s.services.memory_store.list_archives(s.release.id)
    assert doc["location"] == rec.location
    assert Digest.parse(doc["bundle"]) == rec.bundle
    text = s.site.invoke([*args, "--to", str(tmp_path / "cold-2"), "--with-outputs"])
    assert text.exit_code == ExitCode.OK, text.output
    assert "archived verif/cpu 2026.10.1" in text.stdout
    assert len(s.services.memory_store.list_archives(s.release.id)) == 2


# R1: a build planned from uncommitted changes cannot be reproduced from git: refused.
def test_dirty_flow_refused(tmp_path: Path) -> None:
    s = signed_off(tmp_path)
    (s.site.proj / "src" / "a.txt").write_text("edited, not committed\n")
    rebuilt = s.site.invoke(["build"])
    assert rebuilt.exit_code == ExitCode.OK, rebuilt.output
    dirty = create_release(
        s.services.memory_store, name="verif/cpu", version="2", build=find_build(s.site.proj,
        None).build, outputs=["sim.result[*]"], by="alice",
    )  # fmt: skip
    with pytest.raises(ReleaseError, match="uncommitted changes"):
        archive_release(
            s.services.memory_store, s.cas, dirty, to=tmp_path / "cold", repo=s.site.proj,
            git=hermetic_git(), toolchains=s.services.toolchains(s.site.proj), by="alice",
        )  # fmt: skip
    assert not (tmp_path / "cold" / bundle_file_name(dirty)).exists()
