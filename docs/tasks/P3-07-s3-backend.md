# P3-07 — S3 CAS backend

Status: done · Phase: 3 · Depends on: P0-09 · Size: M

## Goal
The CAS can live on S3-compatible object storage (Ceph RGW, MinIO) behind the same protocol.

## Scope (files)
- create `src/ebs/cas/s3.py` (boto3, optional extra `[s3]`); add to `tests/contract/test_cas.py` with a MinIO
  testcontainer; tests `tests/unit/cas/test_s3_keys.py`

## Requirements
- R1 Key layout mirrors fs (`<domain>/blobs/<algo>/<ab>/<cd>/<hex>`), one bucket per domain or prefix per domain (config).
- R2 Uploads use multipart above 64 MiB with per-part checksums; `If-None-Match: *` conditional put where supported
  (first writer wins), else HEAD-then-PUT with content verification.
- R3 `materialize` downloads to scratch (no symlink mode; `local_path` returns None); large downloads are parallel
  ranged GETs.
- R4 Passes the CAS contract suite (except fs-only tests, explicitly marked).

## Tests
`tests/contract/test_cas.py[s3]` (R1–R4), `test_s3_keys.py` (R1), `tests/unit/cas/test_s3_multipart.py` (R2, moto/respx)

## Notes
- Real-server tests use a SeaweedFS 4.48 container (`tests/helpers/s3.py::s3_server`) or any
  endpoint in `EBS_TEST_S3_URL` (e.g. MinIO): MinIO no longer publishes container images (pulls
  of `minio/minio` and `quay.io/minio/minio` fail). SeaweedFS supports `If-None-Match`, checksums
  and multipart, like MinIO.
- Unit tests use the in-memory `FakeS3` at the boto3 boundary instead of moto, which is not a
  listed dependency (respx mocks httpx, not botocore). No dependency was added.
- `[s3]` floor raised to `boto3>=1.35.2`: the first botocore whose S3 model has `IfNoneMatch` on
  `PutObject` and `CompleteMultipartUpload` (checked against 1.35.0/1.35.1/1.35.2).
- `materialize.py` gained a `download(d, fd)` hook so remote backends write blobs with parallel
  ranged GETs; `S3CAS.download` does not rehash (the runner verifies inputs).
- Follow-up: wire `[cas] backend = "s3"` (+ `S3Settings` keys) into `ebs.toml`, the CLI context,
  the runner and the meta service log reader; today only `FsCAS` is constructed there. The runner
  must keep its import set small, so import `ebs.cas.s3` lazily. Buckets need a lifecycle rule
  that aborts incomplete multipart uploads (aborts here are best-effort).
- Follow-up: GC (`ebs gc`) calls `FsCAS.cleanup_tmp`/`iter_objects`; an S3 domain needs the
  `iter_digests`-based path once the backend is wired in.
