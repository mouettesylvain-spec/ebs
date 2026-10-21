"""S3 backend uploads: multipart with per-part checksums, conditional puts (P3-07 R2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas import s3
from ebs.cas.api import ObjectKind
from ebs.cas.s3 import MULTIPART_THRESHOLD, S3CAS, S3Settings
from ebs.core.digest import Digest, StreamingHasher, hash_bytes
from ebs.core.errors import CasError
from ebs.core.tree import TreeEntry, TreeManifest
from tests.helpers.cas import sample_tree
from tests.helpers.s3 import MIN_PART, FakeS3, _error, b64sha256

BIG = bytes(range(256)) * ((2 * MIN_PART + 1000) // 256)  # three 5 MiB parts, the last short


def _cas(client: FakeS3, **kw: object) -> S3CAS:
    settings = S3Settings(**{"bucket": "bkt", **kw})  # type: ignore[arg-type]
    client.create_bucket(Bucket="bkt")
    return S3CAS(client, "dom", settings)


def _small(client: FakeS3) -> S3CAS:
    return _cas(client, multipart_threshold=MIN_PART, part_size=MIN_PART)


# R2
def test_default_threshold_is_64_mib() -> None:
    assert MULTIPART_THRESHOLD == 64 << 20
    assert S3Settings(bucket="bkt").multipart_threshold == 64 << 20


# R2: small objects are one conditional PUT carrying their SHA-256
def test_small_put_is_conditional_with_checksum() -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"hello")
    (put,) = client.ops("put_object")
    assert put["IfNoneMatch"] == "*"
    assert put["ChecksumSHA256"] == b64sha256(b"hello")
    assert put["Key"] == cas.object_key(d, "file")
    assert client.ops("create_multipart_upload") == []


# R2: above the threshold, multipart with a checksum per part
@pytest.mark.parametrize("source", ["file", "bytes"])
def test_large_upload_is_multipart_with_part_checksums(tmp_path: Path, source: str) -> None:
    client = FakeS3()
    cas = _small(client)
    if source == "file":
        path = tmp_path / "big"
        path.write_bytes(BIG)
        d = cas.put_file(path, expected=hash_bytes(BIG))
    else:
        d = cas.put_bytes(BIG)
    assert d == hash_bytes(BIG)
    assert client.ops("put_object") == []
    parts = client.ops("upload_part")
    assert [p["PartNumber"] for p in parts] == [1, 2, 3]
    for p in parts:
        body = p["Body"]
        assert isinstance(body, bytes)
        assert p["ChecksumSHA256"] == b64sha256(body)
    assert [len(p["Body"]) for p in parts] == [MIN_PART, MIN_PART, len(BIG) - 2 * MIN_PART]  # type: ignore[arg-type]
    (complete,) = client.ops("complete_multipart_upload")
    assert complete["IfNoneMatch"] == "*"
    assert client.objects("bkt")[cas.object_key(d, "file")] == BIG


# R2: the boundary — exactly the threshold is one PUT, one byte more is multipart
def test_threshold_boundary(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _small(client)
    at = tmp_path / "at"
    at.write_bytes(b"a" * MIN_PART)
    cas.put_file(at)
    assert len(client.ops("put_object")) == 1
    assert client.ops("create_multipart_upload") == []
    over = tmp_path / "over"
    over.write_bytes(b"b" * (MIN_PART + 1))
    cas.put_file(over)
    assert len(client.ops("create_multipart_upload")) == 1


# R2: first writer wins — an existing object is never replaced, and that is not an error
def test_conditional_put_first_writer_wins() -> None:
    client = FakeS3()
    cas = _cas(client)
    d = cas.put_bytes(b"x")
    client.objects("bkt")[cas.object_key(d, "file")] = b"x"  # (identical, by construction)
    assert cas.put_bytes(b"x") == d
    assert len(client.ops("put_object")) == 2  # the second one got 412 PreconditionFailed


# R2: an existing large object is not uploaded again
def test_multipart_skips_existing_object(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _small(client)
    cas.put_bytes(BIG)
    client.calls.clear()
    path = tmp_path / "big"
    path.write_bytes(BIG)
    cas.put_file(path)
    assert client.ops("create_multipart_upload") == []
    assert client.ops("upload_part") == []


# R2: another writer completes the same digest while we upload: we lose, cleanly
def test_multipart_race_lost_is_aborted_not_an_error() -> None:
    client = FakeS3()
    cas = _small(client)
    key = cas.object_key(hash_bytes(BIG), "file")

    def other_writer(kw: dict[str, object]) -> None:
        client.objects("bkt")[key] = BIG

    client.hooks["complete_multipart_upload"] = other_writer
    assert cas.put_bytes(BIG) == hash_bytes(BIG)
    assert len(client.aborted) == 1
    assert client.uploads == {}


# R2: servers without conditional writes: HEAD-then-PUT, decided once per store
def test_unsupported_conditional_falls_back_to_head_then_put(tmp_path: Path) -> None:
    client = FakeS3(conditional="unsupported")
    cas = _cas(client)
    d = cas.put_bytes(b"one")
    assert client.objects("bkt")[cas.object_key(d, "file")] == b"one"
    cas.put_bytes(b"two")
    puts = client.ops("put_object")
    assert ["IfNoneMatch" in p for p in puts] == [True, False, False]  # one 501, then never again
    assert all(p["ChecksumSHA256"] == b64sha256(p["Body"]) for p in puts)  # type: ignore[arg-type]
    client.calls.clear()
    cas.put_bytes(b"one")  # exists: HEAD answers, no PUT
    assert [op for op, _ in client.calls] == ["head_object"]


def test_conditional_disabled_by_settings() -> None:
    client = FakeS3()
    cas = _cas(client, conditional_put=False)
    cas.put_bytes(b"x")
    cas.put_bytes(b"x")
    assert [op for op, _ in client.calls] == ["head_object", "put_object", "head_object"]


def test_unsupported_conditional_multipart(tmp_path: Path) -> None:
    client = FakeS3(conditional="unsupported")
    cas = _small(client)
    d = cas.put_bytes(BIG)
    assert client.objects("bkt")[cas.object_key(d, "file")] == BIG
    completes = client.ops("complete_multipart_upload")
    assert ["IfNoneMatch" in c for c in completes] == [True, False]


def test_unsupported_conditional_multipart_lost_race() -> None:
    client = FakeS3(conditional="unsupported")
    cas = _small(client)
    key = cas.object_key(hash_bytes(BIG), "file")
    seen: list[int] = []

    def other_writer(kw: dict[str, object]) -> None:
        seen.append(1)
        if len(seen) == 1:
            client.objects("bkt")[key] = BIG

    client.hooks["complete_multipart_upload"] = other_writer
    cas.put_bytes(BIG)
    assert len(client.ops("complete_multipart_upload")) == 1  # the HEAD saw the winner
    assert len(client.aborted) == 1


# R2: a concurrent conditional write (409) falls back to HEAD-then-PUT for that object
def test_conflict_falls_back_once() -> None:
    client = FakeS3()
    cas = _cas(client)
    calls: list[int] = []

    def conflict(kw: dict[str, object]) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise _error("PutObject", "ConditionalRequestConflict", 409)

    client.hooks["put_object"] = conflict
    d = cas.put_bytes(b"x")
    assert client.objects("bkt")[cas.object_key(d, "file")] == b"x"
    cas.put_bytes(b"y")
    assert "IfNoneMatch" in client.ops("put_object")[-1]  # conditional writes stay on


# R2: content verification — a corrupted transfer is refused and nothing becomes visible
def test_corrupted_transfer_is_refused() -> None:
    client = FakeS3()
    cas = _cas(client)

    def corrupt(kw: dict[str, object]) -> None:
        kw["Body"] = b"evil"

    client.hooks["put_object"] = corrupt
    with pytest.raises(CasError, match="BadDigest"):
        cas.put_bytes(b"good")
    assert client.objects("bkt") == {}


# R2 / I8: a file that changes between hashing and upload is never stored under the old digest
def test_file_changed_during_multipart_upload(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _small(client)
    path = tmp_path / "big"
    path.write_bytes(BIG)

    def edit(kw: dict[str, object]) -> None:
        if kw["PartNumber"] == 1:
            with open(path, "r+b") as f:
                f.seek(MIN_PART + 100_000)  # well past any read-ahead of part 1
                f.write(b"EDITED")

    client.hooks["upload_part"] = edit
    with pytest.raises(CasError, match="changed"):
        cas.put_file(path)
    assert client.objects("bkt") == {}
    assert len(client.aborted) == 1
    assert client.ops("complete_multipart_upload") == []


# R2 / I8: a failed part aborts the upload; nothing is visible
def test_part_failure_aborts(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _small(client)

    def fail(kw: dict[str, object]) -> None:
        if kw["PartNumber"] == 2:
            raise _error("UploadPart", "InternalError", 500)

    client.hooks["upload_part"] = fail
    with pytest.raises(CasError, match="InternalError"):
        cas.put_bytes(BIG)
    assert client.objects("bkt") == {}
    assert client.uploads == {}
    assert len(client.aborted) == 1


def test_abort_failure_keeps_original_error() -> None:
    client = FakeS3()
    cas = _small(client)

    def fail(kw: dict[str, object]) -> None:
        raise _error("X", "InternalError", 500)

    client.hooks["upload_part"] = fail
    client.hooks["abort_multipart_upload"] = fail
    with pytest.raises(CasError, match="InternalError"):
        cas.put_bytes(BIG)


def test_put_file_expected_mismatch_uploads_nothing(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _small(client)
    for data in (b"small", BIG):
        path = tmp_path / "f"
        path.write_bytes(data)
        with pytest.raises(CasError, match="digest mismatch"):
            cas.put_file(path, expected=hash_bytes(b"other"))
    assert client.ops("put_object") == []
    assert client.ops("create_multipart_upload") == []


def test_put_file_missing_source(tmp_path: Path) -> None:
    with pytest.raises(CasError, match="cannot store"):
        _cas(FakeS3()).put_file(tmp_path / "absent")


def test_server_error_on_put_is_cas_error() -> None:
    client = FakeS3()
    cas = _cas(client)

    def fail(kw: dict[str, object]) -> None:
        raise _error("PutObject", "AccessDenied", 403)

    client.hooks["put_object"] = fail
    with pytest.raises(CasError, match="AccessDenied"):
        cas.put_bytes(b"x")


# I9 on S3: a manifest is only stored once its children are
def test_put_manifest_requires_children() -> None:
    client = FakeS3()
    cas = _cas(client)
    blob = cas.put_bytes(b"leaf")
    child = TreeManifest((TreeEntry("f", "file", blob, 4, False, None),))
    parent = TreeManifest((TreeEntry("d", "dir", child.digest(), 4, False, None),))
    with pytest.raises(CasError, match="not stored"):
        cas.put_manifest(parent)
    assert cas.put_manifest(child) == child.digest()
    assert cas.put_manifest(parent) == parent.digest()
    client.calls.clear()
    assert cas.put_manifest(parent) == parent.digest()  # already stored: one HEAD
    assert [op for op, _ in client.calls] == ["head_object"]


# I9 on S3: put_tree writes every blob and child manifest before the manifest naming it
def test_put_tree_order(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)
    root = cas.put_tree(sample_tree(tmp_path / "src"))
    written = [str(kw["Key"]) for kw in client.ops("put_object")]
    seen: set[str] = set()
    for key in written:
        if key.endswith(".json"):
            hex_ = key.rsplit("/", 1)[1].removesuffix(".json")
            for entry in cas.get_tree(Digest("sha256", hex_)).entries:
                if entry.digest is not None:
                    kind: ObjectKind = "file" if entry.type == "file" else "tree"
                    assert cas.object_key(entry.digest, kind) in seen, (key, entry.name)
        seen.add(key)
    assert written[-1] == cas.object_key(root, "tree")


# I9 on S3: a failed child manifest leaves no parent manifest visible
def test_put_tree_child_failure_leaves_no_parent(tmp_path: Path) -> None:
    client = FakeS3()
    cas = _cas(client)

    def fail_manifests(kw: dict[str, object]) -> None:
        if str(kw["Key"]).endswith(".json"):
            raise _error("PutObject", "InternalError", 500)

    client.hooks["put_object"] = fail_manifests
    with pytest.raises(CasError, match="InternalError"):
        cas.put_tree(sample_tree(tmp_path / "src"))
    assert [k for k in client.objects("bkt") if k.endswith(".json")] == []
    assert len(client.ops("put_object")) > 1  # blobs went first, then the first manifest failed


# R2 / I8: a small file is stored as exactly the bytes that were hashed, even if it changes
def test_small_put_file_uploads_exactly_the_hashed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = FakeS3()
    cas = _cas(client)
    path = tmp_path / "f"
    path.write_bytes(b"original")

    class EditingHasher(StreamingHasher):
        def finish(self) -> tuple[Digest, int]:
            path.write_bytes(b"EDITED")
            return super().finish()

    monkeypatch.setattr(s3, "StreamingHasher", EditingHasher)
    d = cas.put_file(path)
    assert d == hash_bytes(b"original")
    assert client.objects("bkt")[cas.object_key(d, "file")] == b"original"


# R2: a successful multipart upload is not aborted afterwards
def test_completed_upload_is_not_aborted() -> None:
    client = FakeS3()
    _small(client).put_bytes(BIG)
    assert client.aborted == []


# R2: put_bytes boundary — exactly the threshold is one PUT
def test_put_bytes_threshold_boundary() -> None:
    client = FakeS3()
    cas = _small(client)
    cas.put_bytes(b"t" * MIN_PART)
    assert len(client.ops("put_object")) == 1
    assert client.ops("create_multipart_upload") == []


# R2: S3 allows at most 10,000 parts; bigger objects get bigger parts
def test_part_count_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s3, "_MAX_PARTS", 2)
    client = FakeS3()
    cas = _small(client)
    d = cas.put_bytes(BIG)
    parts = client.ops("upload_part")
    assert len(parts) == 2
    assert len(parts[0]["Body"]) > MIN_PART  # type: ignore[arg-type]
    assert client.objects("bkt")[cas.object_key(d, "file")] == BIG
