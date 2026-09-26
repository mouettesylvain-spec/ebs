# P2-02 — Releases and channels

Status: todo · Phase: 2 · Depends on: P1-09 · Size: L

## Goal
Teams publish selected outputs of a build as immutable, versioned releases and move channels
(`stable`, `nightly`) that downstream teams follow.

## Read first
- docs/architecture.md § "Versioning, releases and provenance" (table + diagram); data-model Phase 2;
  invariant I19; open decision D4

## Scope (files)
- create `src/ebs/release/{releases,channels,export}.py`, `src/ebs/cli/release.py` (`create`, `show`, `list`,
  `yank`, `export --to`), `src/ebs/cli/channel.py` (`set`, `show`, `history`), Alembic revision, service routes
- tests `tests/unit/release/*`, `tests/integration/release/*`

## Requirements
- R1 `ebs release create NAME VERSION --build B --output step.output[selector]…` records the selected output
  ids/digests; the build must be finished and all selected actions passed (or `--allow-failed` with a reason
  stored). Versions validated per D4; (domain, name, version) unique.
- R2 Releases are immutable; `yank` sets yanked fields but never deletes (I19); released objects become GC roots.
- R3 Channels: `set` moves the pointer and appends history; optional gate policy per channel (config): require
  a passing build of a named gate flow that imported this release (checked via provenance edges); the check's
  result is stored in history.
- R4 `export --to DOMAIN` copies blobs/trees into the target domain CAS (never links), creates a release there
  with an `exported_from` reference, and writes an audit row; requires membership in both domains.
- R5 All operations available via service API (for dashboard/CI) with authz from P2-01.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_releases.py::test_create_rules[...]`, `::test_unique` | R1 | unit |
| `test_release_immutability.py::test_no_update_no_delete`, `::test_yank`, `tests/unit/gc/test_roots.py::test_release_roots` | R2 (I19) | unit |
| `test_channels.py::test_history`, `::test_gate_policy[...]` | R3 | unit |
| `tests/integration/release/test_export.py::test_copy_not_link_and_audit` | R4 | integration |
| `tests/integration/release/test_api.py` | R5 | integration |
