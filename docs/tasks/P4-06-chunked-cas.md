# P4-06 — Content-defined chunking for large blobs

Status: todo · Phase: 4 · Depends on: P0-09, P3-07 · Parallel-safe with: P4-01…P4-05, P4-07 · Size: L

## Goal
Large blobs that differ slightly (100 GB databases) share storage: they are split with content-defined chunking
(FastCDC; the restic/casync approach), stored as chunks plus a chunk manifest in the CAS, transparently to `CAS`
users.

## Read first
- docs/architecture.md § "Artifact storage and garbage collection" → Scaling concerns
- docs/design/interfaces.md §5 (CAS); docs/design/invariants.md (CAS invariants, I8/I9)
- Existing code to follow: `src/ebs/cas/{api,fs}.py`

## Scope (files)
- create `src/ebs/cas/chunking.py` (FastCDC, pure), `src/ebs/cas/chunked.py` (wrapper over any `CAS`)
- create `tests/unit/cas/test_chunking.py`, `tests/unit/cas/test_chunked.py`, perf test
  `tests/perf/test_chunk_dedup.py`
- modify `src/ebs/cas/api.py` only if needed (note "CONTRACT CHANGE" and update interfaces.md in the same commit)

## Out of scope
A chunk-aware network protocol, REAPI backend, compression, cross-domain chunk sharing (never allowed).
Garbage-collecting chunks beyond reachability from manifests (P1-09 extended minimally in R6).

## Requirements
- R1 `chunking.py`: deterministic FastCDC (min/avg/max sizes configurable); same bytes ⇒ same cut points,
  and the chunks reassemble to the input exactly.
- R2 Blobs over `chunk_threshold` are stored as chunks `sha256:` plus a chunk manifest; the blob digest stays the
  digest of the full content, so action keys and input ids do not change (no `KEY_SCHEMA_VERSION` bump).
- R3 `get`/`open`/`materialize` of a chunked blob return identical bytes; callers cannot tell it is chunked.
- R4 Dedup: two synthetic 1 GB files differing by a few small edits store at most ~5 % extra bytes.
- R5 Invariants extended to chunks: immutable, tmp → fsync → atomic rename, domain-scoped paths, manifest
  written last so a visible blob is always complete (I8/I9).
- R6 GC treats chunks as reachable iff some live manifest references them; a chunk is never swept while any
  manifest points at it, and a manifest is never visible without all its chunks.
- R7 Corruption: a missing or digest-mismatched chunk on read raises an `EbsError` naming the blob and chunk.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_chunking.py::test_deterministic_cuts`, `::test_reassembles` (Hypothesis) | R1 | unit/property |
| `test_chunked.py::test_blob_digest_unchanged` | R2 | unit |
| `test_chunked.py::test_roundtrip_get_open_materialize` | R3 | unit |
| `tests/perf/test_chunk_dedup.py::test_1gb_small_edits_dedup` | R4 | perf |
| `test_chunked.py::test_atomic_write_and_manifest_last`, `::test_domain_scoped` | R5 | unit |
| `tests/unit/gc/test_chunk_reachability.py` | R6 | unit |
| `test_chunked.py::test_missing_or_corrupt_chunk_errors` | R7 | unit |

## Done when
- [ ] `make check` and `make test-perf` pass (paste summary lines)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] interfaces.md / invariants.md updated for chunked blobs
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Use sparse/synthetic data generated from a seeded PRNG; the perf test never runs under `--cov`. Chunk-size
defaults are config keys, not constants. The S3 backend (P3-07) must pass the same wrapper tests.
