"""S3 backend reads: materialize downloads, parallel ranged GETs (P3-07 R3)."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from botocore.exceptions import EndpointConnectionError

from ebs.cas.s3 import S3CAS, S3Settings
from ebs.core.canon import canonical_json
from ebs.core.digest import hash_bytes
from ebs.core.errors import CasError
from ebs.core.tree import TreeManifest
from tests.helpers.cas import sample_tree
from tests.helpers.s3 import MIN_PART, FakeS3, _error

BIG = bytes(range(256)) * ((2 * MIN_PART + 1000) // 256)


def _cas(client: FakeS3, **kw: object) -> S3CAS:
    settings = S3Settings(**{"bucket": "bkt", "part_size": MIN_PART, **kw})  # type: ignore[arg-type]
    client.create_bucket(Bucket="bkt")
    return S3CAS(client, "dom", settings)


# R3
def test_local_path_is_none() -> None:
    cas = _cas(FakeS3())
    d = cas.put_bytes(b"x")
    assert cas.local_path(d) is None


# R3: no symlink (nor hard-link) mode
@pytest.mark.parametrize("mode", ["symlink", "hardlink"])
def test_link_modes_are_refused(tmp_path: Path, mode: str) -> None:
    cas = _cas(FakeS3())
    d = cas.put_bytes(b"x")
    with pytest.raises(CasError, match="use mode='copy'"):
        cas.materialize(d, "file", tmp_path / "out", mode)  # type: ignore[arg-type]
    assert not os.path.lexists(tmp_path / "out")


# R3: `auto` downloads even above the copy threshold (a symlink would point nowhere)
def test_auto_downloads_regular_files(tmp_path: Path) -> None:
    cas = _cas(FakeS3())
    d = cas.put_tree(sample_tree(tmp_path / "src"))
    dest = tmp_path / "out"
    cas.materialize(d, "tree", dest, copy_threshold=0)
    for path in (dest / "a.txt", dest / "sub" / "deeper" / "c.bin"):
        st = os.lstat(path)
        assert stat.S_ISREG(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o444
    assert (dest / "link").readlink() == Path("sub/b.txt")


# R3: a large blob is fetched as ranged GETs that run concurrently
def test_large_download_is_parallel_ranged_gets(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client, download_workers=3)
    d = cas.put_bytes(BIG)
    # Every ranged GET waits until three are in flight: a sequential download would time out.
    barrier = threading.Barrier(3, timeout=10)

    def rendezvous(kw: dict[str, object]) -> None:
        if "Range" in kw:
            barrier.wait()

    client.hooks["get_object"] = rendezvous
    out = tmp_path / "big"
    cas.materialize(d, "file", out)
    assert out.read_bytes() == BIG
    ranges = sorted(str(kw["Range"]) for kw in client.ops("get_object"))
    assert ranges == sorted(
        [
            f"bytes=0-{MIN_PART - 1}",
            f"bytes={MIN_PART}-{2 * MIN_PART - 1}",
            f"bytes={2 * MIN_PART}-{len(BIG) - 1}",
        ]
    )


# R3: a blob of at most one part is a single plain GET
def test_small_download_is_one_get(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"small")
    cas.materialize(d, "file", tmp_path / "f", executable=True)
    assert (tmp_path / "f").read_bytes() == b"small"
    assert os.stat(tmp_path / "f").st_mode & stat.S_IXUSR
    (get,) = client.ops("get_object")
    assert "Range" not in get


# R3: a short ranged response (object replaced or truncated) fails; the partial file is removed
def test_short_range_response_fails(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(BIG)
    client.range_filter = lambda data: data[:-1]
    with pytest.raises(CasError, match="size mismatch"):
        cas.materialize(d, "file", tmp_path / "big")
    assert not os.path.lexists(tmp_path / "big")


def test_short_single_response_fails(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"abc")

    def shrink(kw: dict[str, object]) -> None:
        client.objects("bkt")[cas.object_key(d, "file")] = b"ab"  # after HEAD said 3 bytes

    client.hooks["get_object"] = shrink
    with pytest.raises(CasError, match="size mismatch"):
        cas.materialize(d, "file", tmp_path / "f")


def test_missing_blob_creates_nothing(tmp_path: Path) -> None:
    cas = _cas(FakeS3())
    with pytest.raises(CasError, match="not found"):
        cas.materialize(hash_bytes(b"absent"), "file", tmp_path / "f")
    assert not os.path.lexists(tmp_path / "f")
    with pytest.raises(CasError, match="not found"):
        cas.materialize(hash_bytes(b"absent"), "tree", tmp_path / "t")
    assert not os.path.lexists(tmp_path / "t")


def test_get_error_mid_download(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(BIG)

    def fail(kw: dict[str, object]) -> None:
        if kw.get("Range") == f"bytes={MIN_PART}-{2 * MIN_PART - 1}":
            raise _error("GetObject", "SlowDown", 503)

    client.hooks["get_object"] = fail
    with pytest.raises(CasError, match="SlowDown"):
        cas.materialize(d, "file", tmp_path / "big")
    assert not os.path.lexists(tmp_path / "big")


# open() streams the object
def test_open_streams(tmp_path: Path) -> None:
    cas = _cas(FakeS3())
    d = cas.put_bytes(BIG)
    with cas.open(d) as f:
        assert f.read(10) == BIG[:10]
        assert f.read() == BIG[10:]
    with pytest.raises(CasError, match="not found"):
        cas.open(hash_bytes(b"absent"))


def test_get_tree_rejects_corruption() -> None:
    client = FakeS3()
    cas = _cas(client)
    empty = cas.put_manifest(TreeManifest(()))
    key = cas.object_key(empty, "tree")
    client.objects("bkt")[key] = b"not json"
    with pytest.raises(CasError, match="corrupt"):
        cas.get_tree(empty)
    assert not cas.verify(empty)
    with pytest.raises(CasError, match="not found"):
        cas.get_tree(hash_bytes(b"absent"))


@pytest.mark.parametrize(
    ("data", "match"),
    [(b"not json", "not valid JSON"), (b'{"v":1,"entries":[{"name":"/"}]}', "invalid")],
)
def test_get_tree_rejects_bad_content_under_its_digest(data: bytes, match: str) -> None:
    client = FakeS3()
    cas = _cas(client)
    d = hash_bytes(data)
    client.objects("bkt")[cas.object_key(d, "tree")] = data
    with pytest.raises(CasError, match=match):
        cas.get_tree(d)


def test_get_tree_rejects_non_canonical_json() -> None:
    client = FakeS3()
    cas = _cas(client)
    data = b'{ "v": 1, "entries": [] }'
    d = hash_bytes(data)
    client.objects("bkt")[cas.object_key(d, "tree")] = data
    with pytest.raises(CasError, match="canonical"):
        cas.get_tree(d)


def test_read_errors_are_cas_errors() -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"x")

    def deny(kw: dict[str, object]) -> None:
        raise _error("X", "AccessDenied", 403)

    for op in ("head_object", "get_object", "delete_object", "list_objects_v2"):
        client.hooks[op] = deny
    with pytest.raises(CasError, match="AccessDenied"):
        cas.has(d)
    with pytest.raises(CasError, match="AccessDenied"):
        cas.open(d)
    with pytest.raises(CasError, match="AccessDenied"):
        cas.verify(d)
    with pytest.raises(CasError, match="AccessDenied"):
        cas.delete(d)
    with pytest.raises(CasError, match="AccessDenied"):
        list(cas.iter_digests())


def test_verify_detects_corrupt_blob() -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"good")
    client.objects("bkt")[cas.object_key(d, "file")] = b"bad"
    assert not cas.verify(d)


@pytest.mark.slow  # boto3 loads its S3 service model (~2 s) on first use
def test_connect_builds_a_boto3_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    settings = S3Settings(bucket="ebs-{domain}", endpoint_url="http://s3.invalid", region="r1")
    cas = S3CAS.connect(settings, "dom")
    assert (cas.domain, cas.bucket) == ("dom", "ebs-dom")


# Transport failures (no HTTP answer at all) are errors, never "missing" or "already stored"
def test_transport_errors_are_cas_errors(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client, multipart_threshold=MIN_PART)
    stored = cas.put_bytes(b"stored")

    def unreachable(kw: dict[str, object]) -> None:
        raise EndpointConnectionError(endpoint_url="http://s3.invalid")

    for op in (
        "put_object",
        "head_object",
        "get_object",
        "create_multipart_upload",
        "list_objects_v2",
        "delete_object",
    ):
        client.hooks[op] = unreachable
    calls: list[Callable[[], object]] = [
        lambda: cas.put_bytes(b"x"),
        lambda: cas.put_bytes(BIG),
        lambda: cas.has(stored),
        lambda: cas.open(stored),
        lambda: cas.verify(stored),
        lambda: cas.get_tree(stored),
        lambda: cas.materialize(stored, "file", tmp_path / "m"),
        lambda: list(cas.iter_digests()),
        lambda: cas.delete(stored),
    ]
    for call in calls:
        with pytest.raises(CasError, match="Could not connect"):
            call()
    client.hooks.clear()
    client.hooks["complete_multipart_upload"] = unreachable
    with pytest.raises(CasError, match="Could not connect"):
        cas.put_bytes(BIG)
    assert client.uploads == {}  # aborted


# R3: an object that grew between HEAD and GET is refused too
@pytest.mark.parametrize("ranged", [False, True])
def test_grown_object_fails(tmp_path: Path, ranged: bool) -> None:
    client = FakeS3()
    cas = _cas(client)
    if ranged:
        d = cas.put_bytes(BIG)
        client.range_filter = lambda data: data + b"X"
    else:
        d = cas.put_bytes(b"abc")
        key = cas.object_key(d, "file")

        def grow(kw: dict[str, object]) -> None:
            client.objects("bkt")[key] = b"abcd"

        client.hooks["get_object"] = grow
    with pytest.raises(CasError, match="size mismatch"):
        cas.materialize(d, "file", tmp_path / "f")
    assert not os.path.lexists(tmp_path / "f")


# R3: exactly one part is still a single plain GET
def test_download_of_exactly_one_part_is_one_get(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    data = b"p" * MIN_PART
    d = cas.put_bytes(data)
    cas.materialize(d, "file", tmp_path / "f")
    assert (tmp_path / "f").read_bytes() == data
    (get,) = client.ops("get_object")
    assert "Range" not in get


# R3: after a failed range, the ranges not yet started are cancelled
def test_failed_range_cancels_the_rest(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client, download_workers=1)
    d = cas.put_bytes(BIG)

    def fail(kw: dict[str, object]) -> None:
        raise _error("GetObject", "SlowDown", 503)

    client.hooks["get_object"] = fail
    with pytest.raises(CasError):
        cas.materialize(d, "file", tmp_path / "f")
    assert len(client.ops("get_object")) == 1


def test_verify_fails_if_either_copy_is_corrupt() -> None:
    client = FakeS3()
    cas = _cas(client)
    tree = cas.put_manifest(TreeManifest(()))
    assert cas.put_bytes(canonical_json(TreeManifest(()).to_json())) == tree  # also a blob
    assert cas.verify(tree)
    client.objects("bkt")[cas.object_key(tree, "file")] = b"bad"
    assert not cas.verify(tree)
