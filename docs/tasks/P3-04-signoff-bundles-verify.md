# P3-04 — Sign-off bundles and `ebs verify`

Status: review · Phase: 3 · Depends on: P3-03, P2-03 · Size: M

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

## Notes
- Started while P3-03 was still `review` in the index: its code (ebs.release.attest/sign) is
  merged on main, which this branch is based on.
- New dependency `zstandard` (R1 asks for zstd): Python < 3.14 has no zstd module and the `zstd`
  CLI is not on every host. Determinism holds per zstandard version (single-threaded, level 10);
  the manifest's member digests are the stable identity across versions.
- CONTRACT CHANGE: `ReleaseStore.record_archive` / `list_archives`, `ArchiveRecord`, routes
  `POST|GET /releases/{id}/archives`, table `archives` (revision 0015, append-only trigger) with a
  `created_by` column the data model sketch did not have.
- `ebs verify NAME VERSION` verifies the release's latest archived bundle (its file must still
  have the recorded digest); a release without an archive is refused with the archive command.
  Re-planning uses only the bundle: flow from `git/flow.bundle`, toolchains from the records,
  import globs against the bundled tree manifests, sources rehashed into a scratch CAS, refined
  with the recorded producer output ids.
- Only the flow repository is bundled: the plan records one repository (sources are flow-relative
  globs). A build planned from a dirty tree or outside git is refused.
- `git bundle` is made in a scratch bare repository borrowing the source's objects, so the
  user's repository is never written (no temporary refs).
- `ebs release import` restores the release's own build (same UUID, actions, results, final plan)
  and release; imported releases of the lock closure are not re-created as records (their records
  and plans stay in the bundle), nor are the build's `release_imports` rows. Restored rows get
  new ids and creation times. A restore interrupted after the build was recorded can be run
  again: the build is then recorded under a new UUID (stores cannot look builds up by UUID).
- Bundle file names omit the domain (and `/` becomes `_`): a clash is refused, never
  overwritten; archive releases of different domains to different directories.
- Test changes: `test_release_immutability.py::test_no_update_no_delete` lists `record_archive` as
  an allowed write (it appends an `archives` row, never changes a release);
  `test_migrations.py` P3_TABLES gains `archives`; the authz matrix and pagination route lists
  gain the archive routes.
- Follow-ups: (1) restore the lock closure's releases as records too (needs their builds'
  results in the bundle); (2) `ebs verify` of a release without an archive by building a
  temporary bundle from the live system; (3) register the bundled toolchain records on import
  (P3-05 re-validation may want them); (4) P3-05 can reuse `replan` with
  `--toolchain-override`.
