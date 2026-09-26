# P3-07 — S3 CAS backend

Status: todo · Phase: 3 · Depends on: P0-09 · Size: M

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
