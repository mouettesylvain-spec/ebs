"""S3 backend key layout and bucket selection (P3-07 R1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.cas.s3 import S3CAS, S3Settings
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import CasError
from ebs.core.tree import TreeManifest
from tests.helpers.s3 import FakeS3

HEX = "ab" + "cd" + "0" * 60


def _cas(client: FakeS3, domain: str = "cpu-nda", **kw: object) -> S3CAS:
    settings = S3Settings(**kw)  # type: ignore[arg-type]
    cas = S3CAS(client, domain, settings)
    client.create_bucket(Bucket=cas.bucket)
    return cas


# R1
@pytest.mark.parametrize("algo", ["sha256", "blake3"])
def test_keys_mirror_fs_layout(tmp_path: Path, algo: str) -> None:
    cas = _cas(FakeS3(), bucket="ebs-cas")
    fs = FsCAS(tmp_path, "cpu-nda")
    d = Digest(algo, HEX)  # type: ignore[arg-type]
    assert cas.object_key(d, "file") == f"cpu-nda/blobs/{algo}/ab/cd/{HEX}"
    assert cas.object_key(d, "tree") == f"cpu-nda/trees/{algo}/ab/cd/{HEX}.json"
    assert cas.object_key(d, "file") == fs.blob_path(d).relative_to(tmp_path).as_posix()
    assert cas.object_key(d, "tree") == fs.tree_path(d).relative_to(tmp_path).as_posix()


# R1: prefix per domain in one shared bucket
def test_prefix_per_domain_in_shared_bucket() -> None:
    client = FakeS3()
    a = _cas(client, "a", bucket="ebs-cas", prefix="site1/cas")
    b = _cas(client, "b", bucket="ebs-cas", prefix="site1/cas/")
    assert a.bucket == b.bucket == "ebs-cas"
    d = a.put_bytes(b"nda")
    ab, cd, hex_ = d.shard()
    assert list(client.objects("ebs-cas")) == [f"site1/cas/a/blobs/sha256/{ab}/{cd}/{hex_}"]
    assert not b.has(d)
    assert list(b.iter_digests()) == []
    b.delete(d)  # cannot reach the other domain's object
    assert a.has(d)


# R1: one bucket per domain
def test_bucket_per_domain() -> None:
    client = FakeS3()
    a = _cas(client, "cpu-nda", bucket="ebs-{domain}")
    b = _cas(client, "gpu-nda", bucket="ebs-{domain}")
    assert (a.bucket, b.bucket) == ("ebs-cpu-nda", "ebs-gpu-nda")
    d = a.put_bytes(b"x")
    assert list(client.objects("ebs-cpu-nda")) == [a.object_key(d, "file")]
    assert client.objects("ebs-gpu-nda") == {}
    assert not b.has(d)


# R1
@pytest.mark.parametrize(
    ("domain", "settings", "match"),
    [
        ("../x", {"bucket": "bkt"}, "invalid CAS domain"),
        ("", {"bucket": "bkt"}, "invalid CAS domain"),
        ("Upper_Case", {"bucket": "ebs-{domain}"}, "not a valid bucket"),  # valid domain though
        ("d", {"bucket": ""}, "bucket is empty"),
        ("d", {"bucket": "b"}, "not a valid bucket"),
        ("d", {"bucket": "ebs-{project}"}, "placeholder"),
        ("d", {"bucket": "bkt", "prefix": "a/../b"}, "prefix"),
        ("d", {"bucket": "bkt", "prefix": "a//b"}, "prefix"),
        ("d", {"bucket": "bkt", "part_size": 1 << 20}, "part_size"),
        ("d", {"bucket": "bkt", "part_size": (5 << 30) + 1}, "part_size"),
        ("d", {"bucket": "bkt", "multipart_threshold": 0}, "multipart_threshold"),
        ("d", {"bucket": "bkt", "download_workers": 0}, "download_workers"),
    ],
)
def test_invalid_configuration(domain: str, settings: dict[str, object], match: str) -> None:
    with pytest.raises(CasError, match=match):
        S3CAS(FakeS3(), domain, S3Settings(**settings))  # type: ignore[arg-type]


def test_forced_invalid_digest_is_rejected() -> None:
    cas = _cas(FakeS3(), bucket="bkt")
    bad = Digest("sha256", HEX)
    object.__setattr__(bad, "hex", "../../etc")
    with pytest.raises(CasError):
        cas.object_key(bad, "file")


# R1: GC enumerates the domain's keys only, across pages, and skips foreign keys
def test_iter_digests_parses_keys_and_paginates() -> None:
    client = FakeS3(page_size=2)
    cas = _cas(client, bucket="bkt", prefix="p")
    blobs = {cas.put_bytes(bytes([i]) * (i + 1)) for i in range(4)}
    tree = cas.put_manifest(TreeManifest(()))
    objects = client.objects("bkt")
    objects["p/cpu-nda/blobs/sha256/zz/zz/notadigest"] = b"?"
    objects[f"p/cpu-nda/blobs/sha256/00/00/{HEX}"] = b"?"  # wrong shard
    objects[f"p/cpu-nda/blobs/md5/ab/cd/{HEX}"] = b"?"
    objects[f"p/cpu-nda/trees/sha256/ab/cd/{HEX}"] = b"?"  # no .json suffix
    objects["p/cpu-nda/stray"] = b"?"
    objects["p/other/blobs/sha256/ab/cd/" + HEX] = b"?"
    found = dict(cas.iter_digests())
    assert set(found) == blobs | {tree}
    assert found[hash_bytes(b"\x03" * 4)] == 4
    assert len(client.ops("list_objects_v2")) > 2  # really paginated
