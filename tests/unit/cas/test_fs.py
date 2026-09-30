"""FsCAS layout, domain isolation, reads, verification and tmp cleanup (task P0-09)."""

from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes, hash_file
from ebs.core.errors import CasError
from ebs.core.tree import build_tree
from tests.helpers.cas import sample_tree, write

HEX = st.from_regex(r"[0-9a-f]{64}", fullmatch=True)


def test_fs_cas_satisfies_protocol(cas: FsCAS) -> None:
    backend: CAS = cas
    assert backend.domain == "test"


# R1
def test_layout(cas: FsCAS, tmp_path: Path) -> None:
    base = tmp_path / "cas" / "test"
    d = cas.put_bytes(b"hello")
    ab, cd, hex_ = d.shard()
    blob = base / "blobs" / "sha256" / ab / cd / hex_
    assert blob.is_file()
    assert blob.read_bytes() == b"hello"
    assert stat.S_IMODE(blob.stat().st_mode) == 0o444
    assert cas.blob_path(d) == blob
    assert cas.local_path(d) == blob

    t = cas.put_tree(sample_tree(tmp_path / "src"))
    ab, cd, hex_ = t.shard()
    manifest = base / "trees" / "sha256" / ab / cd / f"{hex_}.json"
    assert manifest.is_file()
    assert cas.tree_path(t) == manifest
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o444
    assert cas.local_path(t) is None  # trees have no single local file to bind
    assert (base / "tmp").is_dir()
    assert sorted(p.name for p in base.iterdir()) == ["blobs", "tmp", "trees"]


# R1
def test_relative_root_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    cas = FsCAS(Path("rel"), "test")
    d = cas.put_bytes(b"x")
    assert cas.blob_path(d).is_absolute()
    assert cas.blob_path(d).is_relative_to(tmp_path / "rel" / "test")


# R1
def test_domain_isolation_paths(tmp_path: Path) -> None:
    a = FsCAS(tmp_path, "cpu-nda")
    b = FsCAS(tmp_path, "gpu-nda")
    d = a.put_bytes(b"secret")
    t = a.put_tree(sample_tree(tmp_path / "src"))
    assert a.has(d)
    assert a.has(t)
    assert not b.has(d)
    assert not b.has(t)
    assert b.local_path(d) is None
    assert not b.verify(d)
    with pytest.raises(CasError, match="gpu-nda"):
        b.open(d)
    with pytest.raises(CasError, match="gpu-nda"):
        b.get_tree(t)
    assert a.blob_path(d).is_relative_to(tmp_path / "cpu-nda")
    assert b.blob_path(d).is_relative_to(tmp_path / "gpu-nda")
    assert list(b.iter_digests()) == []


# R1
@pytest.mark.parametrize("domain", ["", ".", "..", "a/b", "../x", "x\x00y", "/abs", "a b", "-x"])
def test_invalid_domain_rejected(tmp_path: Path, domain: str) -> None:
    with pytest.raises(CasError, match="domain"):
        FsCAS(tmp_path, domain)


# R1
@given(hex_=HEX, algo=st.sampled_from(["sha256", "blake3"]))
def test_every_digest_path_stays_in_domain_root(hex_: str, algo: str) -> None:
    root = Path("/nonexistent/cas")
    cas = FsCAS(root, "dom")
    d = Digest(algo, hex_)  # type: ignore[arg-type]
    for path in (cas.blob_path(d), cas.tree_path(d)):
        assert Path(os.path.normpath(path)) == path
        assert path.is_relative_to(root / "dom")


# R1
@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("hex", "../../../../etc/passwd"),
        ("hex", "a" * 63 + "/"),
        ("hex", "ab/../../x" + "0" * 54),
        ("hex", "a" * 64 + "/../../../x"),  # valid prefix: needs a full match, not a prefix one
        ("algo", "../../../../x"),
        ("algo", "md5"),
    ],
)
def test_tampered_digest_cannot_escape(cas: FsCAS, attr: str, value: str) -> None:
    bad = hash_bytes(b"x")
    object.__setattr__(bad, attr, value)
    for op in (cas.has, cas.open, cas.verify, cas.delete, cas.local_path, cas.get_tree):
        with pytest.raises(CasError, match="invalid digest"):
            op(bad)
    for path_of in (cas.blob_path, cas.tree_path):
        with pytest.raises(CasError, match="invalid digest"):
            path_of(bad)
    with pytest.raises(CasError, match="invalid digest"):
        cas.put_file(Path(__file__), expected=bad)


def test_non_digest_rejected(cas: FsCAS) -> None:
    with pytest.raises(CasError, match="invalid digest"):
        cas.has("sha256:" + "0" * 64)  # type: ignore[arg-type]


def test_put_bytes_roundtrip(cas: FsCAS) -> None:
    d = cas.put_bytes(b"payload")
    assert d == hash_bytes(b"payload")
    assert cas.has(d)
    with cas.open(d) as f:
        assert f.read() == b"payload"
    assert cas.put_bytes(b"payload") == d
    assert cas.put_bytes(b"") == hash_bytes(b"")


def test_put_file_streams_large_file(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", "test", chunk_size=4096)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(4096 * 5 + 17))
    d = cas.put_file(src)
    assert d == hash_file(src)[0]
    assert cas.blob_path(d).read_bytes() == src.read_bytes()


def test_put_file_with_blake3_expected(cas: FsCAS, tmp_path: Path) -> None:
    pytest.importorskip("blake3", reason="missing dependency: optional blake3 extra")
    src = tmp_path / "f"
    src.write_bytes(b"data")
    expected = hash_file(src, "blake3")[0]
    assert cas.put_file(src, expected=expected) == expected
    assert cas.verify(expected)


def test_put_file_missing_source(cas: FsCAS, tmp_path: Path) -> None:
    with pytest.raises(CasError, match="nope"):
        cas.put_file(tmp_path / "nope")
    with pytest.raises(CasError, match="directory"):
        cas.put_file(tmp_path)
    assert [p for p in (tmp_path / "cas").rglob("*") if p.is_file()] == []


def test_invalid_chunk_size(tmp_path: Path) -> None:
    with pytest.raises(CasError, match="chunk"):
        FsCAS(tmp_path, "test", chunk_size=0)


# R5
def test_expected_mismatch(cas: FsCAS, tmp_path: Path) -> None:
    src = tmp_path / "f.txt"
    src.write_bytes(b"actual bytes")
    wrong = hash_bytes(b"other bytes")
    with pytest.raises(CasError) as exc:
        cas.put_file(src, expected=wrong)
    msg = str(exc.value)
    assert str(wrong) in msg
    assert str(hash_bytes(b"actual bytes")) in msg
    assert str(src) in msg
    base = tmp_path / "cas" / "test"
    assert not cas.has(wrong)
    assert not cas.has(hash_bytes(b"actual bytes"))
    assert [p for p in base.rglob("*") if p.is_file()] == []
    assert cas.put_file(src, expected=hash_bytes(b"actual bytes")) == hash_bytes(b"actual bytes")


# R6
def test_verify_detects_corruption(cas: FsCAS, tmp_path: Path) -> None:
    d = cas.put_bytes(b"good")
    assert cas.verify(d)
    blob = cas.blob_path(d)
    blob.chmod(0o644)
    blob.write_bytes(b"evil")
    assert not cas.verify(d)
    assert not cas.verify(hash_bytes(b"absent"))

    t = cas.put_tree(sample_tree(tmp_path / "src"))
    assert cas.verify(t)
    manifest = cas.tree_path(t)
    manifest.chmod(0o644)
    manifest.write_bytes(manifest.read_bytes().replace(b"a.txt", b"b.txt"))
    assert not cas.verify(t)


# R6
def test_get_tree_validates(cas: FsCAS, tmp_path: Path) -> None:
    src = sample_tree(tmp_path / "src")
    t = cas.put_tree(src)
    expected_digest, manifests = build_tree(src, hash_file)
    assert t == expected_digest
    assert cas.get_tree(t) == manifests[t]

    with pytest.raises(CasError, match="not found"):
        cas.get_tree(hash_bytes(b"no such tree"))

    # Content that no longer matches its digest.
    path = cas.tree_path(t)
    path.chmod(0o644)
    path.write_bytes(path.read_bytes().replace(b'"a.txt"', b'"A.txt"'))
    with pytest.raises(CasError, match="corrupt"):
        cas.get_tree(t)


# R6
@pytest.mark.parametrize(
    ("doc", "reason"),
    [
        (b'{"v":2,"entries":[]}', "version"),
        (b"not json", "JSON"),
        (b'{"entries":[],"v":1.0}', "version"),
        (
            b'{"entries":[{"name":"b","type":"symlink","target":"x"},'
            b'{"name":"a","type":"symlink","target":"x"}],"v":1}',
            "sorted",
        ),
        (b'{"v":1, "entries":[]}', "canonical"),
    ],
)
def test_get_tree_rejects_invalid_manifest(cas: FsCAS, doc: bytes, reason: str) -> None:
    # Stored under its own digest, so only the P0-03 manifest rules can reject it.
    d = hash_bytes(doc)
    path = cas.tree_path(d)
    path.parent.mkdir(parents=True)
    path.write_bytes(doc)
    with pytest.raises(CasError, match=reason):
        cas.get_tree(d)


def test_open_missing(cas: FsCAS) -> None:
    with pytest.raises(CasError, match="not found"):
        cas.open(hash_bytes(b"missing"))


def test_delete_and_iter_digests(cas: FsCAS, tmp_path: Path) -> None:
    d1 = cas.put_bytes(b"one")
    d2 = cas.put_bytes(b"three")
    t = cas.put_tree(sample_tree(tmp_path / "src"))
    listed = dict(cas.iter_digests())
    assert listed[d1] == 3
    assert listed[d2] == 5
    assert listed[t] == len(canonical_json(cas.get_tree(t).to_json()))
    cas.delete(d1)
    cas.delete(t)
    cas.delete(hash_bytes(b"never stored"))  # no-op
    assert not cas.has(d1)
    assert not cas.has(t)
    assert cas.has(d2)
    listed = dict(cas.iter_digests())
    assert d1 not in listed
    assert t not in listed
    assert d2 in listed


def test_iter_digests_skips_foreign_files(cas: FsCAS, tmp_path: Path) -> None:
    d = cas.put_bytes(b"one")
    base = tmp_path / "cas" / "test"
    write(base / "blobs" / "sha256" / "zz" / "yy" / "junk", b"x")
    write(base / "blobs" / "sha256" / "stray-file", b"x")
    write(base / "blobs" / "md5" / "ab" / "cd" / ("0" * 64), b"x")
    write(base / "trees" / "sha256" / "ab" / "cd" / "notadigest.json", b"x")
    write(base / "trees" / "sha256" / "ab" / "cd" / ("0" * 64), b"x")  # no .json suffix
    # A digest filed under the wrong shard is not reported either.
    write(base / "blobs" / "sha256" / "00" / "00" / d.hex, b"one")
    # A directory where an object should be is not an object.
    (base / "blobs" / "sha256" / "ab" / "cd" / ("abcd" + "0" * 60)).mkdir(parents=True)
    assert list(cas.iter_digests()) == [(d, 3)]


def test_iter_digests_empty(cas: FsCAS) -> None:
    assert list(cas.iter_digests()) == []


# R8
def test_cleanup_tmp(tmp_path: Path, fake_clock: FakeClock) -> None:
    cas = FsCAS(tmp_path / "cas", "test", clock=fake_clock)
    assert cas.cleanup_tmp() == 0  # no tmp dir yet
    cas.put_bytes(b"make tmp exist")
    tmp = tmp_path / "cas" / "test" / "tmp"
    now = fake_clock.now().timestamp()
    stale = tmp / "stale.tmp"
    fresh = tmp / "fresh.tmp"
    stale_dir = tmp / "stale-dir"
    for p in (stale, fresh):
        p.write_bytes(b"partial")
    stale_dir.mkdir()
    (stale_dir / "inner").write_bytes(b"x")
    os.utime(stale, (now - 25 * 3600, now - 25 * 3600))
    os.utime(stale_dir, (now - 25 * 3600, now - 25 * 3600))
    os.utime(fresh, (now - 23 * 3600, now - 23 * 3600))
    assert cas.cleanup_tmp() == 2
    assert sorted(p.name for p in tmp.iterdir()) == ["fresh.tmp"]
    fake_clock.advance(2 * 3600)
    assert cas.cleanup_tmp() == 1
    assert list(tmp.iterdir()) == []
    fresh.write_bytes(b"again")
    os.utime(fresh, (now, now))
    assert cas.cleanup_tmp(max_age=timedelta(hours=1)) == 1


# R8
def test_cleanup_tmp_does_not_touch_objects(tmp_path: Path, fake_clock: FakeClock) -> None:
    cas = FsCAS(tmp_path / "cas", "test", clock=fake_clock)
    d = cas.put_bytes(b"keep me")
    blob = cas.blob_path(d)
    old = fake_clock.now().timestamp() - 90 * 86400
    os.utime(blob, (old, old))
    assert cas.cleanup_tmp() == 0
    assert cas.verify(d)


# R8: the default age is exactly 24 h, so a slow live upload's staging file is kept.
def test_cleanup_tmp_default_boundary(tmp_path: Path, fake_clock: FakeClock) -> None:
    cas = FsCAS(tmp_path / "cas", "test", clock=fake_clock)
    cas.put_bytes(b"make tmp exist")
    tmp = tmp_path / "cas" / "test" / "tmp"
    now = fake_clock.now().timestamp()
    young, old = tmp / "young.tmp", tmp / "old.tmp"
    for p, age in ((young, 24 * 3600 - 60), (old, 24 * 3600 + 60)):
        p.write_bytes(b"partial")
        os.utime(p, (now - age, now - age))
    assert cas.cleanup_tmp() == 1
    assert [p.name for p in tmp.iterdir()] == ["young.tmp"]


# R8: a writer renaming its staging file away mid-scan is not an error.
def test_cleanup_tmp_races_with_writer(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cas = FsCAS(tmp_path / "cas", "test", clock=fake_clock)
    cas.put_bytes(b"make tmp exist")
    tmp = tmp_path / "cas" / "test" / "tmp"
    (tmp / "vanishing.tmp").write_bytes(b"x")
    real_scandir = os.scandir

    def scandir_then_rename(path: Any) -> Any:
        it = real_scandir(path)
        entries = list(it)
        it.close()
        for e in entries:
            os.unlink(e.path)  # the writer's rename lands between listing and stat
        return _ListIter(entries)

    monkeypatch.setattr(os, "scandir", scandir_then_rename)
    assert cas.cleanup_tmp(max_age=timedelta(0)) == 0


def test_cleanup_tmp_permission_error_wrapped(
    tmp_path: Path, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    cas = FsCAS(tmp_path / "cas", "test", clock=fake_clock)
    cas.put_bytes(b"make tmp exist")
    stale = tmp_path / "cas" / "test" / "tmp" / "stale.tmp"
    stale.write_bytes(b"x")
    old = fake_clock.now().timestamp() - 48 * 3600
    os.utime(stale, (old, old))

    def denied(path: Any) -> None:
        raise PermissionError(13, "Permission denied", os.fspath(path))

    monkeypatch.setattr(os, "unlink", denied)
    with pytest.raises(CasError, match=r"stale\.tmp.*Permission denied"):
        cas.cleanup_tmp()


class _ListIter:
    """Stands in for os.scandir's context-manager iterator over a fixed list."""

    def __init__(self, entries: list[os.DirEntry[str]]) -> None:
        self._entries = entries

    def __enter__(self) -> list[os.DirEntry[str]]:
        return self._entries

    def __exit__(self, *exc: object) -> None:
        return None


def test_put_bytes_multi_chunk(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", "test", chunk_size=4)
    data = bytes(range(10))  # three chunks, the last one partial
    d = cas.put_bytes(data)
    assert d == hash_bytes(data)
    assert cas.blob_path(d).read_bytes() == data


# R6: I/O errors other than "missing" are reported as CasError naming the path.
def test_read_errors_wrapped(cas: FsCAS, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = cas.put_bytes(b"blob")
    t = cas.put_tree(sample_tree(tmp_path / "src"))

    def eio(self: Path, *args: Any, **kwargs: Any) -> Any:
        raise OSError(5, "Input/output error", os.fspath(self))

    monkeypatch.setattr(Path, "read_bytes", eio)
    with pytest.raises(CasError, match="Input/output error"):
        cas.get_tree(t)
    monkeypatch.undo()

    real_open = open

    def eio_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path) == os.fspath(cas.blob_path(d)):
            raise OSError(5, "Input/output error", os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", eio_open)
    with pytest.raises(CasError, match=r"Input/output error"):
        cas.verify(d)
