"""S3 test doubles: an in-memory fake client and a real S3-compatible server (P3-07).

`FakeS3` stands in for a boto3 S3 client at the network boundary. It implements only the calls
`ebs.cas.s3` makes, raises botocore `ClientError`s with the codes real servers use, verifies
`ChecksumSHA256` headers like S3 does, records every call, and can play an old server without
conditional writes (`conditional="unsupported"`). It is thread-safe (ranged downloads run in a
pool).

`s3_server` is a session fixture with a real server: `EBS_TEST_S3_URL` (any S3-compatible
endpoint, e.g. MinIO, with credentials in `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) or, if
unset, a SeaweedFS container via testcontainers (MinIO no longer publishes container images).
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import pytest
from botocore.exceptions import ClientError

from ebs.core.digest import hash_bytes

MIN_PART = 5 << 20  # S3's minimum size of every part but the last

_MISSING = (
    "missing dependency: an S3-compatible server (set EBS_TEST_S3_URL=http://host:port with "
    "AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or make Docker available for testcontainers)"
)
_IMAGE = "chrislusf/seaweedfs:4.48"


def _error(op: str, code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


def b64sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


@dataclass
class _Upload:
    bucket: str
    key: str
    parts: dict[int, bytes] = field(default_factory=dict)


class FakeS3:
    """In-memory S3 client (the subset `S3CAS` uses). Buckets must be created first."""

    def __init__(
        self,
        *,
        conditional: Literal["supported", "unsupported"] = "supported",
        page_size: int = 1000,
    ) -> None:
        self.conditional = conditional
        self.page_size = page_size
        self.buckets: dict[str, dict[str, bytes]] = {}
        self.uploads: dict[str, _Upload] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.aborted: list[str] = []
        # Test hooks: run before an operation's effect (may raise to inject a failure).
        self.hooks: dict[str, Callable[[dict[str, object]], None]] = {}
        # Replaces a ranged GET's bytes (e.g. to truncate a response).
        self.range_filter: Callable[[bytes], bytes] | None = None
        self._lock = threading.Lock()

    # -- helpers ------------------------------------------------------------------------------

    def create_bucket(self, **kw: object) -> Mapping[str, object]:
        self.buckets.setdefault(str(kw["Bucket"]), {})
        return {}

    def objects(self, bucket: str) -> dict[str, bytes]:
        return self.buckets[bucket]

    def ops(self, name: str) -> list[dict[str, object]]:
        return [kw for op, kw in self.calls if op == name]

    def assert_content_addressed(self) -> None:
        """Every CAS key in every bucket holds bytes that hash to the digest it names."""
        for bucket, objects in self.buckets.items():
            for key, data in objects.items():
                parts = key.split("/")
                if len(parts) >= 5 and parts[-5] in {"blobs", "trees"}:
                    algo, hex_ = parts[-4], parts[-1].removesuffix(".json")
                    actual = hash_bytes(data, algo)  # type: ignore[arg-type]
                    assert actual.hex == hex_, f"s3://{bucket}/{key} holds other bytes"

    def _upload(self, kw: dict[str, object], op: str) -> _Upload:
        # Like S3: an upload id is only valid for the bucket and key it was started on.
        upload = self.uploads.get(str(kw["UploadId"]))
        if upload is None or (upload.bucket, upload.key) != (kw["Bucket"], kw["Key"]):
            raise _error(op, "NoSuchUpload", 404)
        return upload

    def _enter(self, op: str, kw: dict[str, object]) -> dict[str, bytes]:
        with self._lock:
            self.calls.append((op, kw))
        hook = self.hooks.get(op)
        if hook is not None:
            hook(kw)
        bucket = self.buckets.get(str(kw["Bucket"]))
        if bucket is None:
            raise _error(op, "NoSuchBucket", 404)
        return bucket

    def _conditional(self, op: str, kw: dict[str, object], bucket: dict[str, bytes]) -> None:
        if "IfNoneMatch" not in kw:
            return
        if self.conditional == "unsupported":
            raise _error(op, "NotImplemented", 501)
        assert kw["IfNoneMatch"] == "*"
        if str(kw["Key"]) in bucket:
            raise _error(op, "PreconditionFailed", 412)

    # -- objects ------------------------------------------------------------------------------

    def head_object(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("head_object", kw)
        data = bucket.get(str(kw["Key"]))
        if data is None:
            raise _error("HeadObject", "404", 404)
        return {"ContentLength": len(data)}

    def get_object(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("get_object", kw)
        data = bucket.get(str(kw["Key"]))
        if data is None:
            raise _error("GetObject", "NoSuchKey", 404)
        rng = kw.get("Range")
        if rng is not None:
            start, end = str(rng).removeprefix("bytes=").split("-")
            data = data[int(start) : int(end) + 1]
            if self.range_filter is not None:
                data = self.range_filter(data)
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def put_object(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("put_object", kw)
        body = kw["Body"]
        assert isinstance(body, bytes)
        checksum = kw.get("ChecksumSHA256")
        if checksum is not None and checksum != b64sha256(body):
            raise _error("PutObject", "BadDigest", 400)
        with self._lock:
            self._conditional("PutObject", kw, bucket)
            bucket[str(kw["Key"])] = body
        return {}

    def delete_object(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("delete_object", kw)
        bucket.pop(str(kw["Key"]), None)
        return {}

    def list_objects_v2(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("list_objects_v2", kw)
        prefix = str(kw.get("Prefix", ""))
        start = str(kw.get("ContinuationToken", ""))
        keys = sorted(k for k in bucket if k.startswith(prefix) and k > start)
        page = keys[: self.page_size]
        out: dict[str, object] = {
            "Contents": [{"Key": k, "Size": len(bucket[k])} for k in page],
            "IsTruncated": len(keys) > len(page),
        }
        if len(keys) > len(page):
            out["NextContinuationToken"] = page[-1]
        if not page:
            del out["Contents"]  # like S3: no Contents key on an empty page
        return out

    # -- multipart ----------------------------------------------------------------------------

    def create_multipart_upload(self, **kw: object) -> Mapping[str, object]:
        self._enter("create_multipart_upload", kw)
        assert kw.get("ChecksumAlgorithm") == "SHA256"
        upload_id = secrets.token_hex(8)
        self.uploads[upload_id] = _Upload(str(kw["Bucket"]), str(kw["Key"]))
        return {"UploadId": upload_id}

    def upload_part(self, **kw: object) -> Mapping[str, object]:
        self._enter("upload_part", kw)
        upload = self._upload(kw, "UploadPart")
        body = kw["Body"]
        assert isinstance(body, bytes)
        if kw.get("ChecksumSHA256") != b64sha256(body):
            raise _error("UploadPart", "BadDigest", 400)
        number = kw["PartNumber"]
        assert isinstance(number, int)
        upload.parts[number] = body
        return {"ETag": f'"{hashlib.md5(body).hexdigest()}"'}

    def complete_multipart_upload(self, **kw: object) -> Mapping[str, object]:
        bucket = self._enter("complete_multipart_upload", kw)
        upload = self._upload(kw, "CompleteMultipartUpload")
        listing = kw["MultipartUpload"]
        assert isinstance(listing, dict)
        parts = listing["Parts"]
        numbers = [p["PartNumber"] for p in parts]
        assert numbers == sorted(upload.parts) == list(range(1, len(numbers) + 1))
        for p in parts:
            body = upload.parts[p["PartNumber"]]
            assert p["ChecksumSHA256"] == b64sha256(body)
            assert p["ETag"] == f'"{hashlib.md5(body).hexdigest()}"'
        sizes = [len(upload.parts[n]) for n in numbers]
        if any(size < MIN_PART for size in sizes[:-1]):
            raise _error("CompleteMultipartUpload", "EntityTooSmall", 400)
        with self._lock:
            self._conditional("CompleteMultipartUpload", kw, bucket)
            bucket[upload.key] = b"".join(upload.parts[n] for n in numbers)
            del self.uploads[str(kw["UploadId"])]
        return {}

    def abort_multipart_upload(self, **kw: object) -> Mapping[str, object]:
        self._enter("abort_multipart_upload", kw)
        self.uploads.pop(str(kw["UploadId"]), None)
        self.aborted.append(str(kw["UploadId"]))
        return {}


@dataclass(frozen=True)
class S3Server:
    endpoint_url: str
    access_key: str
    secret_key: str

    def client(self) -> Any:
        import boto3

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name="us-east-1",
        )


@pytest.fixture(scope="session")
def s3_server() -> Iterator[S3Server]:
    """A real S3-compatible endpoint (see the module docstring)."""
    url = os.environ.get("EBS_TEST_S3_URL")
    if url:
        yield S3Server(
            url,
            os.environ.get("AWS_ACCESS_KEY_ID", "ebs-test"),
            os.environ.get("AWS_SECRET_ACCESS_KEY", "ebs-test"),
        )
        return
    try:
        from testcontainers.core.container import DockerContainer
    except ImportError:  # pragma: no cover - dev extra always installs it
        pytest.skip(_MISSING)
    container = (
        DockerContainer(_IMAGE).with_command("server -s3 -dir=/data").with_exposed_ports(8333)
    )
    try:
        container.start()
    except Exception as exc:  # docker missing or not running
        pytest.skip(f"{_MISSING}: {exc}")
    try:
        host = container.get_container_host_ip()
        server = S3Server(f"http://{host}:{container.get_exposed_port(8333)}", "ebs-test", "x")
        _wait_ready(server)
        yield server
    finally:
        container.stop()


def _wait_ready(server: S3Server) -> None:
    from tests.helpers.wait import wait_until

    client = server.client()

    def ready() -> bool:
        try:
            client.list_buckets()
        except Exception:
            return False
        return True

    wait_until(ready, timeout=60, interval=0.5, what=f"S3 server at {server.endpoint_url}")
