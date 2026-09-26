# P3-04 — Sign-off bundles and `ebs verify`

Status: todo · Phase: 3 · Depends on: P3-03, P2-03 · Size: M

## Goal
A self-contained archive that lets a release be re-validated years later without the live system, and a
check that an exact rerun would reproduce the same action keys.

## Read first
- docs/architecture.md § "Versioning…" → Long-term reproducibility (items 1 and 3)

## Scope (files)
- create `src/ebs/release/archive.py`, `verify.py`; CLI `ebs release archive`, `ebs verify`; tests
  `tests/unit/release/test_archive.py`, `test_verify.py`, `tests/integration/test_archive_restore.py`

## Requirements
- R1 Bundle (tar + zstd, deterministic: sorted entries, fixed mtimes/uids) contains: release record + attestation,
  plan.json, `git bundle` of each flow/source repo at the recorded commits, lock closure (recursively imported
  releases' records and plans), toolchain records with captured env, optional outputs (`--with-outputs`), manifest
  with digests of all members.
- R2 `ebs release archive --to PATH` writes to cold storage path and records it in `archives`.
- R3 `ebs verify <release|bundle>` re-plans from the bundle (sources from git bundles, toolchains from records) and
  checks every action key matches the recorded one, without executing anything; mismatches are reported with
  `diff_plans` field reasons.
- R4 Restoring a bundle into an empty CAS + empty DB (import command) makes `ebs verify` pass.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_archive.py::test_deterministic_bytes`, `::test_members_manifest` | R1 | unit |
| `test_archive.py::test_records_location` | R2 | unit |
| `test_verify.py::test_keys_match`, `::test_mismatch_reasons` | R3 | unit |
| `tests/integration/test_archive_restore.py` | R4 | integration |
