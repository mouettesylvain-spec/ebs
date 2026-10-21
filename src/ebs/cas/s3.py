"""S3 CAS backend: the CAS protocol on S3-compatible object storage (Ceph RGW, MinIO; P3-07).

Keys mirror the fs layout (interfaces.md § 5), below an optional prefix:

    <prefix>/<domain>/blobs/<algo>/<ab>/<cd>/<hex>
    <prefix>/<domain>/trees/<algo>/<ab>/<cd>/<hex>.json

`S3Settings.bucket` is either one shared bucket (a prefix per domain) or a template containing
`{domain}` (one bucket per domain, so bucket policies can follow the NDA scope). Buckets are the
deployment's; the backend never creates one.

S3 writes are atomic per object, so there is no tmp/ area: a PUT, or a multipart upload once
completed, appears whole (I8). Objects up to `multipart_threshold` are one PUT; larger ones are a
multipart upload whose parts each carry their SHA-256, and the source is rehashed while it uploads
so a file edited meanwhile is aborted instead of stored under a stale digest. Writes are
conditional (`If-None-Match: *`, first writer wins). A server that answers 501 to that is
switched, for this store, to HEAD-then-PUT; the content is still verified by the server through
the `ChecksumSHA256` header (concurrent writers of a digest carry identical bytes anyway).

There are no local paths (`local_path` is None), so `materialize` always downloads; blobs larger
than one part are fetched as parallel ranged GETs written in place with `pwrite`.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Final, Literal, Protocol, cast

from ebs.cas.api import DEFAULT_COPY_THRESHOLD, MaterializeMode, ObjectKind
from ebs.cas.fs import _DOMAIN_RE
from ebs.cas.materialize import materialize
from ebs.core.canon import canonical_json
from ebs.core.digest import ALGOS, Algo, Digest, StreamingHasher, hash_bytes
from ebs.core.errors import CasError, TreeError
from ebs.core.log import get_logger
from ebs.core.tree import TreeManifest, build_tree

if TYPE_CHECKING:
    from _typeshed import WriteableBuffer

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError as exc:  # pragma: no cover - the [s3] extra is installed wherever this runs
    raise CasError(
        "the S3 CAS backend needs boto3: install ebs with the s3 extra (pip install 'ebs[s3]')"
    ) from exc

_log = get_logger(__name__)

MULTIPART_THRESHOLD: Final = 64 << 20  # bytes; larger objects are multipart uploads (R2)
_MIN_PART: Final = 5 << 20  # S3 limits: every part but the last is 5 MiB .. 5 GiB
_MAX_PART: Final = 5 << 30
_MAX_PARTS: Final = 10_000
_CHUNK: Final = 1 << 20
_HEX_RE: Final = re.compile(r"[0-9a-f]{64}")
_BUCKET_RE: Final = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
_MISSING: Final = frozenset({"404", "NoSuchKey", "NotFound"})
_PRECONDITION: Final = frozenset({"412", "PreconditionFailed"})
_CONFLICT: Final = frozenset({"409", "ConditionalRequestConflict"})
_UNSUPPORTED: Final = frozenset({"501", "NotImplemented"})
_S3_ERRORS: Final = (ClientError, BotoCoreError)
_Area = Literal["blobs", "trees"]
_AREAS: Final[tuple[tuple[_Area, str], ...]] = (("blobs", ""), ("trees", ".json"))
_KINDS: Final[tuple[ObjectKind, ...]] = ("file", "tree")


class S3Client(Protocol):
    """The subset of a boto3 S3 client this backend calls (keyword arguments as in boto3)."""

    def head_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def get_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def put_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def delete_object(self, **kwargs: object) -> Mapping[str, object]: ...
    def list_objects_v2(self, **kwargs: object) -> Mapping[str, object]: ...
    def create_multipart_upload(self, **kwargs: object) -> Mapping[str, object]: ...
    def upload_part(self, **kwargs: object) -> Mapping[str, object]: ...
    def complete_multipart_upload(self, **kwargs: object) -> Mapping[str, object]: ...
    def abort_multipart_upload(self, **kwargs: object) -> Mapping[str, object]: ...


class _Body(Protocol):
    def read(self, n: int = -1, /) -> bytes: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class S3Settings:
    """Where and how a domain's objects are stored. Credentials come from the standard AWS
    chain (environment, `~/.aws`, instance roles), never from ebs config."""

    bucket: str  # one shared bucket, or a template with "{domain}" (one bucket per domain)
    prefix: str = ""  # key prefix inside the bucket, like the fs CAS root
    endpoint_url: str | None = None  # Ceph RGW / MinIO; None = AWS
    region: str | None = None
    multipart_threshold: int = MULTIPART_THRESHOLD
    part_size: int = MULTIPART_THRESHOLD  # upload part and download range size
    download_workers: int = 8  # parallel ranged GETs per large blob
    conditional_put: bool = True  # False: always HEAD-then-PUT (servers that ignore If-None-Match)

    def __post_init__(self) -> None:
        if not self.bucket:
            raise CasError("S3 CAS: bucket is empty; set the bucket name (or 'name-{domain}')")
        if "{" in self.bucket.replace("{domain}", "") or "}" in self.bucket.replace("{domain}", ""):
            raise CasError(
                f"S3 CAS: bucket {self.bucket!r} has a placeholder other than {{domain}}"
            )
        if "{domain}" not in self.bucket:
            self._check_bucket(self.bucket)
        parts = self.prefix.strip("/").split("/") if self.prefix.strip("/") else []
        if any(p in {"", ".", ".."} for p in parts):
            raise CasError(
                f"S3 CAS: prefix {self.prefix!r} has an empty, '.' or '..' component; use a "
                "plain path such as 'site/cas'"
            )
        if not _MIN_PART <= self.part_size <= _MAX_PART:
            raise CasError(
                f"S3 CAS: part_size {self.part_size} is outside S3's limits (5 MiB .. 5 GiB)"
            )
        if self.multipart_threshold < 1:
            raise CasError(
                f"S3 CAS: multipart_threshold must be positive, got {self.multipart_threshold}"
            )
        if self.download_workers < 1:
            raise CasError(f"S3 CAS: download_workers must be ≥ 1, got {self.download_workers}")

    @property
    def key_prefix(self) -> str:
        stripped = self.prefix.strip("/")
        return f"{stripped}/" if stripped else ""

    def bucket_for(self, domain: str) -> str:
        bucket = self.bucket.replace("{domain}", domain)
        self._check_bucket(bucket)
        return bucket

    @staticmethod
    def _check_bucket(bucket: str) -> None:
        if _BUCKET_RE.fullmatch(bucket) is None:
            raise CasError(
                f"S3 CAS: {bucket!r} is not a valid bucket name (3-63 characters: lowercase "
                "letters, digits, '.', '-'); with one bucket per domain the domain name must "
                "fit too, else use a shared bucket with a prefix per domain"
            )


class S3CAS:
    """CAS in an S3 bucket, scoped to one domain (one bucket, or one key prefix, per domain)."""

    def __init__(
        self, client: S3Client, domain: str, settings: S3Settings, *, algo: Algo = "sha256"
    ) -> None:
        if not isinstance(domain, str) or _DOMAIN_RE.fullmatch(domain) is None:
            raise CasError(
                f"invalid CAS domain {domain!r}: use letters, digits, '.', '_' or '-', starting "
                "with a letter or digit (it becomes a key prefix in the S3 CAS)"
            )
        self.domain = domain
        self.settings = settings
        self.bucket = settings.bucket_for(domain)
        self._root = f"{settings.key_prefix}{domain}/"
        self._client = client
        self._algo: Algo = algo
        self._conditional = settings.conditional_put

    @classmethod
    def connect(cls, settings: S3Settings, domain: str, *, algo: Algo = "sha256") -> S3CAS:
        """A store on a boto3 client built from `settings` and the standard AWS credentials."""
        import boto3  # the [s3] extra
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            config=Config(
                retries={"mode": "standard"},
                max_pool_connections=max(10, settings.download_workers),
            ),
        )
        return cls(client, domain, settings, algo=algo)

    # -- keys -------------------------------------------------------------------------------

    def object_key(self, d: Digest, kind: ObjectKind) -> str:
        """`<prefix><domain>/blobs/<algo>/<ab>/<cd>/<hex>` (`trees/…/<hex>.json` for manifests)."""
        if (
            not isinstance(d, Digest)
            or d.algo not in ALGOS
            or not isinstance(d.hex, str)
            or _HEX_RE.fullmatch(d.hex) is None
        ):
            raise CasError(
                f"invalid digest {d!r} for CAS domain {self.domain!r}: expected a Digest with "
                "64 lowercase hex characters"
            )
        ab, cd, hex_ = d.shard()
        if kind == "tree":
            return f"{self._root}trees/{d.algo}/{ab}/{cd}/{hex_}.json"
        return f"{self._root}blobs/{d.algo}/{ab}/{cd}/{hex_}"

    # -- writes -----------------------------------------------------------------------------

    def put_bytes(self, data: bytes) -> Digest:
        d = hash_bytes(data, self._algo)
        key = self.object_key(d, "file")
        if len(data) <= self.settings.multipart_threshold:
            self._put_object(key, data)
        else:
            view = memoryview(data)

            def parts(size: int) -> Iterator[bytes]:
                for i in range(0, len(view), size):
                    yield view[i : i + size].tobytes()

            self._put_multipart(key, len(data), parts, d, f"<{len(data)} bytes>")
        return d

    def put_file(self, path: Path, *, expected: Digest | None = None) -> Digest:
        return self._put_file(path, expected)[0]

    def _put_file(self, path: Path, expected: Digest | None = None) -> tuple[Digest, int]:
        if expected is not None:
            self.object_key(expected, "file")  # validate before reading anything
        algo: Algo = expected.algo if expected is not None else self._algo
        threshold = self.settings.multipart_threshold
        try:
            with open(path, "rb") as src:
                head = src.read(threshold + 1)
                hasher = StreamingHasher(algo)
                hasher.update(head)
                if len(head) > threshold:
                    while block := src.read(_CHUNK):
                        hasher.update(block)
            d, size = hasher.finish()
            if expected is not None and d != expected:
                raise CasError(
                    f"digest mismatch storing {path} in CAS domain {self.domain!r}: expected "
                    f"{expected}, got {d}; the input changed or the declared digest is wrong"
                )
            key = self.object_key(d, "file")
            if size <= threshold:
                self._put_object(key, head)  # exactly the bytes that were hashed
                return d, size

            def parts(part: int) -> Iterator[bytes]:
                with open(path, "rb") as f:
                    while chunk := f.read(part):
                        yield chunk

            self._put_multipart(key, size, parts, d, path)
            return d, size
        except OSError as exc:
            raise CasError(
                f"cannot store {path} in CAS domain {self.domain!r}: {exc.strerror or exc}"
            ) from exc

    def put_tree(self, root: Path) -> Digest:
        # build_tree uploads every file through _put_file, so blobs land before any manifest.
        root_digest, manifests = build_tree(root, self._put_file)
        self._put_manifests(root_digest, manifests, set())
        return root_digest

    def put_manifest(self, manifest: TreeManifest) -> Digest:
        digest = manifest.digest()
        if self._exists(self.object_key(digest, "tree")):
            return digest  # immutable; its children were checked when it was first stored
        for entry in manifest.entries:
            if entry.digest is None:
                continue
            kind: ObjectKind = "file" if entry.type == "file" else "tree"
            if not self._exists(self.object_key(entry.digest, kind)):
                raise CasError(
                    f"cannot store tree manifest {digest} in CAS domain {self.domain!r}: its "
                    f"{entry.type} entry {entry.name!r} ({entry.digest}) is not stored; store "
                    "blobs and child manifests before their parent"
                )
        self._put_object(self.object_key(digest, "tree"), canonical_json(manifest.to_json()))
        return digest

    def _put_manifests(
        self, d: Digest, manifests: dict[Digest, TreeManifest], written: set[Digest]
    ) -> None:
        """Children before parents, so a visible manifest never references a missing one (I9)."""
        if d in written:
            return
        manifest = manifests[d]
        for entry in manifest.entries:
            if entry.type == "dir" and entry.digest is not None:
                self._put_manifests(entry.digest, manifests, written)
        self._put_object(self.object_key(d, "tree"), canonical_json(manifest.to_json()))
        written.add(d)

    def _put_object(self, key: str, data: bytes) -> None:
        self._write(
            "upload", key, self._client.put_object, Body=data, ChecksumSHA256=_b64sha256(data)
        )

    def _put_multipart(
        self,
        key: str,
        size: int,
        chunks: Callable[[int], Iterator[bytes]],
        digest: Digest,
        source: object,
    ) -> None:
        if self._exists(key):
            return  # first writer wins; do not move 100 GB to learn that
        part_size = max(self.settings.part_size, -(-size // _MAX_PARTS))
        started = self._call(
            "start multipart upload",
            key,
            self._client.create_multipart_upload,
            ChecksumAlgorithm="SHA256",
        )
        upload_id = str(started["UploadId"])
        completed = False
        try:
            hasher = StreamingHasher(digest.algo)
            parts: list[dict[str, object]] = []
            for number, chunk in enumerate(chunks(part_size), 1):
                hasher.update(chunk)
                checksum = _b64sha256(chunk)
                resp = self._call(
                    f"upload part {number}",
                    key,
                    self._client.upload_part,
                    UploadId=upload_id,
                    PartNumber=number,
                    Body=chunk,
                    ChecksumSHA256=checksum,
                )
                parts.append(
                    {"ETag": resp["ETag"], "PartNumber": number, "ChecksumSHA256": checksum}
                )
            if hasher.finish()[0] != digest:
                raise CasError(
                    f"{source} changed while it was uploaded to CAS domain {self.domain!r} (it "
                    f"no longer hashes to {digest}); nothing was stored: retry once it is stable"
                )
            completed = self._write(
                "complete multipart upload",
                key,
                self._client.complete_multipart_upload,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        finally:
            if not completed:  # failed, or another writer won: drop the stored parts
                self._abort(key, upload_id)

    def _write(
        self, action: str, key: str, op: Callable[..., Mapping[str, object]], **kw: object
    ) -> bool:
        """Create `key` unless it exists; True if this call wrote it (first writer wins)."""
        if self._conditional:
            try:
                op(Bucket=self.bucket, Key=key, IfNoneMatch="*", **kw)
                return True
            except ClientError as exc:
                code = _code(exc)
                if code in _PRECONDITION:
                    return False
                if code in _UNSUPPORTED:
                    self._conditional = False
                    _log.warning("cas.s3_no_conditional_put", bucket=self.bucket)
                elif code not in _CONFLICT:  # 409: a concurrent write of this digest
                    raise self._fail(action, key, exc) from exc
            except BotoCoreError as exc:
                raise self._fail(action, key, exc) from exc
        if self._exists(key):
            return False
        self._call(action, key, op, **kw)
        return True

    def _abort(self, key: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except _S3_ERRORS as exc:  # the bucket's lifecycle rule reaps leftovers
            _log.warning("cas.s3_abort_failed", key=key, upload_id=upload_id, error=str(exc))

    # -- reads ------------------------------------------------------------------------------

    def has(self, d: Digest) -> bool:
        return self._exists(self.object_key(d, "file")) or self._exists(self.object_key(d, "tree"))

    def open(self, d: Digest) -> BinaryIO:
        body = self._get(self.object_key(d, "file"), d, "blob")
        return io.BufferedReader(_BodyReader(body), _CHUNK)

    def local_path(self, d: Digest) -> Path | None:
        return None

    def get_tree(self, d: Digest) -> TreeManifest:
        key = self.object_key(d, "tree")
        with contextlib.closing(self._get(key, d, "tree")) as body:
            data = body.read()
        where = f"s3://{self.bucket}/{key}"
        if hash_bytes(data, d.algo) != d:
            raise CasError(f"tree manifest {where} is corrupt: its content does not hash to {d}")
        try:
            manifest = TreeManifest.from_json(json.loads(data))
        except ValueError as exc:
            raise CasError(f"tree manifest {where} is not valid JSON: {exc}") from None
        except TreeError as exc:
            raise CasError(f"tree manifest {where} is invalid: {exc}") from None
        if manifest.digest(d.algo) != d:
            raise CasError(
                f"tree manifest {where} is not in canonical form, so it does not re-encode to {d}"
            )
        return manifest

    def verify(self, d: Digest) -> bool:
        found = False
        for kind in _KINDS:
            key = self.object_key(d, kind)
            try:
                body = self._get(key, d, kind)
            except _NotFound:
                continue
            hasher = StreamingHasher(d.algo)
            with contextlib.closing(body):
                while block := body.read(_CHUNK):
                    hasher.update(block)
            if hasher.finish()[0] != d:
                return False
            found = True
        return found

    def materialize(
        self,
        d: Digest,
        kind: ObjectKind,
        dest: Path,
        mode: MaterializeMode = "auto",
        copy_threshold: int = DEFAULT_COPY_THRESHOLD,
        *,
        writable: bool = False,
        executable: bool = False,
    ) -> None:
        materialize(
            self, d, kind, dest, mode, copy_threshold, writable=writable, executable=executable
        )

    def download(self, d: Digest, fd: int) -> None:
        """Write blob `d` into the empty file open as `fd`; one GET up to a part, else
        parallel ranged GETs (R3). CasError if the blob is missing or changes size meanwhile.
        Like the fs backend's copies, the bytes are not rehashed here: the runner verifies
        input digests before executing."""
        key = self.object_key(d, "file")
        size = self._size(key, d)
        part = self.settings.part_size
        if size <= part:
            self._fetch(key, d, fd, 0, size)
            return
        os.ftruncate(fd, size)
        ranges = [(start, min(start + part, size)) for start in range(0, size, part)]
        workers = min(self.settings.download_workers, len(ranges))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ebs-s3-get") as pool:
            futures = [pool.submit(self._fetch, key, d, fd, a, b, ranged=True) for a, b in ranges]
            try:
                for future in futures:
                    future.result()
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    def _fetch(
        self, key: str, d: Digest, fd: int, start: int, end: int, *, ranged: bool = False
    ) -> None:
        extra: dict[str, object] = {"Range": f"bytes={start}-{end - 1}"} if ranged else {}
        offset = start
        with contextlib.closing(self._get(key, d, "blob", **extra)) as body:
            while chunk := body.read(_CHUNK):
                view = memoryview(chunk)
                while view:
                    n = os.pwrite(fd, view, offset)
                    view = view[n:]
                    offset += n
        if offset != end:
            raise CasError(
                f"size mismatch reading s3://{self.bucket}/{key} (bytes {start}..{end - 1}, got "
                f"{offset - start}): the object changed or the connection dropped; retry"
            )

    # -- GC support -------------------------------------------------------------------------

    def delete(self, d: Digest) -> None:
        for kind in _KINDS:
            self._call("delete", self.object_key(d, kind), self._client.delete_object)

    def iter_digests(self) -> Iterator[tuple[Digest, int]]:
        for area, suffix in _AREAS:
            prefix = f"{self._root}{area}/"
            for key, size in self._list(prefix):
                d = _parse_key(key[len(prefix) :], suffix)
                if d is None:
                    _log.warning("cas.foreign_entry", bucket=self.bucket, key=key)
                    continue
                yield d, size

    def _list(self, prefix: str) -> Iterator[tuple[str, int]]:
        token: str | None = None
        while True:
            kw: dict[str, object] = {"Prefix": prefix}
            if token is not None:
                kw["ContinuationToken"] = token
            try:
                page = self._client.list_objects_v2(Bucket=self.bucket, **kw)
            except _S3_ERRORS as exc:
                raise self._fail("listing", prefix, exc) from exc
            for obj in cast(list[Mapping[str, object]], page.get("Contents", [])):
                yield str(obj["Key"]), cast(int, obj["Size"])
            if not page.get("IsTruncated"):
                return
            token = str(page["NextContinuationToken"])

    # -- S3 calls ---------------------------------------------------------------------------

    def _call(
        self, action: str, key: str, op: Callable[..., Mapping[str, object]], **kw: object
    ) -> Mapping[str, object]:
        try:
            return op(Bucket=self.bucket, Key=key, **kw)
        except _S3_ERRORS as exc:
            raise self._fail(action, key, exc) from exc

    def _exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _MISSING:
                return False
            raise self._fail("lookup", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("lookup", key, exc) from exc
        return True

    def _size(self, key: str, d: Digest) -> int:
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _code(exc) in _MISSING:
                raise self._not_found(d, "blob", key) from None
            raise self._fail("lookup", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("lookup", key, exc) from exc
        return cast(int, resp["ContentLength"])

    def _get(self, key: str, d: Digest, what: str, **kw: object) -> _Body:
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key, **kw)
        except ClientError as exc:
            if _code(exc) in _MISSING:
                raise self._not_found(d, what, key) from None
            raise self._fail("download", key, exc) from exc
        except BotoCoreError as exc:
            raise self._fail("download", key, exc) from exc
        return cast(_Body, resp["Body"])

    def _not_found(self, d: Digest, what: str, key: str) -> CasError:
        return _NotFound(
            f"{what} {d} not found in CAS domain {self.domain!r} (s3://{self.bucket}/{key}); it "
            "may have been garbage-collected or never uploaded: rebuild the producing action"
        )

    def _fail(self, action: str, key: str, exc: Exception) -> CasError:
        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {})
            detail = f"{err.get('Code', '?')}: {err.get('Message', '')}"
        else:
            detail = str(exc)
        return CasError(
            f"S3 {action} of s3://{self.bucket}/{key} failed for CAS domain {self.domain!r}: "
            f"{detail}; check the endpoint, the credentials and the bucket policy"
        )


class _NotFound(CasError):
    """A missing object (a CasError, so callers need not care about the distinction)."""


class _BodyReader(io.RawIOBase):
    """Raw stream over a botocore StreamingBody, for `io.BufferedReader`."""

    def __init__(self, body: _Body) -> None:
        self._body = body

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer, /) -> int:
        view = memoryview(buffer).cast("B")
        data = self._body.read(len(view))
        view[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._body.close()
        super().close()


def _code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _b64sha256(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def _parse_key(rel: str, suffix: str) -> Digest | None:
    """`<algo>/<ab>/<cd>/<hex><suffix>` -> Digest; None for anything else."""
    parts = rel.split("/")
    if len(parts) != 4 or not parts[3].endswith(suffix):
        return None
    algo, ab, cd, name = parts
    hex_ = name.removesuffix(suffix) if suffix else name
    if algo not in ALGOS or _HEX_RE.fullmatch(hex_) is None or (hex_[:2], hex_[2:4]) != (ab, cd):
        return None
    return Digest(cast(Algo, algo), hex_)
