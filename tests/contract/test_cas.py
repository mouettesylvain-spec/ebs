"""CAS contract suite: backend-agnostic behaviour every `CAS` implementation must show.

Parametrized over backends: `fs`, `s3-fake` (S3CAS on the in-memory FakeS3, unit) and `s3`
(S3CAS on a real S3-compatible server, integration; P3-07). Tests that only make sense for a
backend with local paths are marked `fs_only`.
"""

from __future__ import annotations

import os
import secrets
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ebs.cas.api import CAS
from ebs.cas.fs import FsCAS
from ebs.cas.s3 import S3CAS, S3Settings
from ebs.core.digest import hash_bytes, hash_file
from ebs.core.errors import CasError
from ebs.core.tree import TreeEntry, TreeManifest, build_tree
from tests.helpers.cas import sample_tree
from tests.helpers.s3 import FakeS3, S3Server

BACKENDS = ["fs", "s3-fake", pytest.param("s3", marks=pytest.mark.integration)]


@pytest.fixture(params=BACKENDS)
def make(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[str], CAS]]:
    backend: str = request.param
    if backend != "fs" and request.node.get_closest_marker("fs_only") is not None:
        pytest.skip(f"fs-only contract test: the {backend} backend has no local paths")
    if backend == "fs":
        yield lambda domain: FsCAS(tmp_path / "cas", domain)
    elif backend == "s3-fake":
        fake = FakeS3(page_size=3)

        def make_fake(domain: str) -> CAS:
            # One bucket per domain here; the real server below uses a prefix per domain.
            cas = S3CAS(fake, domain, S3Settings(bucket="ebs-{domain}"))
            fake.create_bucket(Bucket=cas.bucket)
            return cas

        yield make_fake
        fake.assert_content_addressed()
    else:
        yield _real_s3(request, tmp_path, monkeypatch)


def _real_s3(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[str], CAS]:
    server: S3Server = request.getfixturevalue("s3_server")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", server.access_key)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", server.secret_key)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    settings = S3Settings(
        bucket=request.getfixturevalue("s3_bucket"),
        prefix=f"cas-{secrets.token_hex(6)}",  # per test, in the session's one bucket
        endpoint_url=server.endpoint_url,
        region="us-east-1",
        multipart_threshold=5 << 20,
        part_size=5 << 20,
    )
    return lambda domain: S3CAS.connect(settings, domain)


def test_domain_attribute(make: Callable[[str], CAS]) -> None:
    assert make("alpha").domain == "alpha"


def test_put_has_open(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"blob")
    assert d == hash_bytes(b"blob")
    assert cas.has(d)
    with cas.open(d) as f:
        assert f.read() == b"blob"
    assert not cas.has(hash_bytes(b"other"))


def test_idempotent_put(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    assert cas.put_bytes(b"x") == cas.put_bytes(b"x")
    assert [d for d, _ in cas.iter_digests()] == [hash_bytes(b"x")]


# R1 / I13-style scoping at the storage level
def test_domains_do_not_share_objects(make: Callable[[str], CAS]) -> None:
    a, b = make("a"), make("b")
    d = a.put_bytes(b"nda")
    assert not b.has(d)
    with pytest.raises(CasError):
        b.open(d)


# R5
def test_put_file_expected(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    src = tmp_path / "f"
    src.write_bytes(b"bytes")
    with pytest.raises(CasError):
        cas.put_file(src, expected=hash_bytes(b"not these"))
    assert list(cas.iter_digests()) == []
    assert cas.put_file(src, expected=hash_bytes(b"bytes")) == hash_bytes(b"bytes")


# R4 / R6
def test_tree_roundtrip(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    src = sample_tree(tmp_path / "src")
    d = cas.put_tree(src)
    expected, manifests = build_tree(src, hash_file)
    assert d == expected
    assert cas.has(d)
    assert cas.verify(d)
    assert cas.get_tree(d) == manifests[d]
    for manifest in manifests.values():
        for entry in manifest.entries:
            if entry.digest is not None:
                assert cas.has(entry.digest)


# R6
def test_verify(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"ok")
    assert cas.verify(d)
    assert not cas.verify(hash_bytes(b"absent"))


# R7
@pytest.mark.parametrize("writable", [False, True])
def test_materialize(make: Callable[[str], CAS], tmp_path: Path, writable: bool) -> None:
    cas = make("test")
    d = cas.put_tree(sample_tree(tmp_path / "src"))
    dest = tmp_path / "out"
    cas.materialize(d, "tree", dest, writable=writable)
    assert (dest / "sub" / "deeper" / "c.bin").read_bytes() == bytes(range(256)) * 8
    assert os.stat(dest / "run.sh").st_mode & stat.S_IXUSR
    assert bool(os.stat(dest / "a.txt").st_mode & stat.S_IWUSR) == writable
    f = tmp_path / "single"
    cas.materialize(cas.put_bytes(b"one"), "file", f)
    assert f.read_bytes() == b"one"


def test_delete(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    d = cas.put_bytes(b"gone")
    cas.delete(d)
    assert not cas.has(d)
    cas.delete(d)


# R4: large objects take the multipart / ranged path on S3 backends
def test_large_object_roundtrip(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    data = os.urandom((5 << 20) * 2 + 12345)
    src = tmp_path / "big"
    src.write_bytes(data)
    d = cas.put_file(src)
    assert d == hash_bytes(data)
    assert cas.verify(d)
    assert dict(cas.iter_digests()) == {d: len(data)}
    out = tmp_path / "out"
    cas.materialize(d, "file", out, "copy")
    assert out.read_bytes() == data


# R4: a second writer of the same digest succeeds and changes nothing
def test_concurrent_writers_same_digest(make: Callable[[str], CAS]) -> None:
    a, b = make("test"), make("test")
    d = a.put_bytes(b"same")
    assert b.put_bytes(b"same") == d
    assert [x for x, _ in b.iter_digests()] == [d]


@pytest.mark.fs_only
def test_local_path_and_symlink_mode(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    d = cas.put_bytes(b"linked")
    local = cas.local_path(d)
    assert local is not None
    out = tmp_path / "link"
    cas.materialize(d, "file", out, "symlink")
    assert out.readlink() == local


def test_delete_tree_manifest(make: Callable[[str], CAS], tmp_path: Path) -> None:
    cas = make("test")
    d = cas.put_tree(sample_tree(tmp_path / "src"))
    cas.delete(d)
    assert not cas.has(d)
    assert d not in dict(cas.iter_digests())
    with pytest.raises(CasError):
        cas.get_tree(d)


# I9: a manifest naming a blob that is not stored is refused
def test_put_manifest_requires_blobs(make: Callable[[str], CAS]) -> None:
    cas = make("test")
    missing = hash_bytes(b"absent")
    manifest = TreeManifest((TreeEntry("f", "file", missing, 6, False, None),))
    with pytest.raises(CasError, match="not stored"):
        cas.put_manifest(manifest)
    assert not cas.has(manifest.digest())


# put_file(expected=...) stores under the expected digest's algorithm
def test_put_file_expected_algo(make: Callable[[str], CAS], tmp_path: Path) -> None:
    pytest.importorskip("blake3", reason="missing dependency: the blake3 extra")
    cas = make("test")
    src = tmp_path / "f"
    src.write_bytes(b"b3")
    d = cas.put_file(src, expected=hash_bytes(b"b3", "blake3"))
    assert d.algo == "blake3"
    assert cas.has(d)
    assert cas.verify(d)
