# P2-03 — Imports and `flow.lock`

Status: todo · Phase: 2 · Depends on: P2-02 · Size: M

## Goal
Flows consume other teams' releases by channel or version; the exact releases are pinned in a reviewed
`flow.lock`, so every build is reproducible and teams upgrade on their own schedule.

## Read first
- docs/architecture.md § "Flow description format" (imports, explicitness rule 4), § "Versioning…"

## Scope (files)
- create `src/ebs/release/lock.py`, `src/ebs/cli/lock.py` (`ebs lock update [ALIAS…]`, `ebs lock check`);
  planner support for `${imports.alias}` (modify `src/ebs/plan/planner.py`, `src/ebs/flow/interp.py` resolver)
- tests `tests/unit/release/test_lock.py`, `tests/unit/plan/test_imports.py`, `tests/integration/test_rtl_to_verif.py`

## Requirements
- R1 `flow.lock` format: canonical-ish YAML (sorted keys, one release per alias: domain, name, version, release
  digest, output ids); lock digest recorded in plan.json.
- R2 Planning with an import requires a lock entry; a missing or stale entry (flow says `channel: stable` but the
  lock pins a release not matching the requested name) is an error suggesting `ebs lock update`; the planner
  never resolves channels itself (only `lock update` does).
- R3 `${imports.rtl}/**/*.sv` resolves to the release's output trees; imported objects are inputs by id (no
  re-hashing of released content); materialization reads from the (possibly exported) CAS of the consumer domain.
- R4 `lock update` resolves channels/versions, shows a diff (old → new version, changed outputs), and writes the
  file; `lock check` exits non-zero if the lock would change (for CI).
- R5 Builds record `release_imports` rows (for `used-by`).
- R6 Integration: RTL flow → release → channel stable → verif flow `lock update` → build uses it; moving the
  channel does not change the verif build until `lock update`.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_lock.py::test_format_roundtrip_stable` | R1 | unit |
| `test_imports.py::test_missing_or_stale_lock[...]` | R2 | unit |
| `test_imports.py::test_import_globs_resolve_by_id` | R3 | unit |
| `test_lock.py::test_update_diff`, `::test_check_exit_code` | R4 | unit |
| `test_imports.py::test_release_imports_recorded` | R5 | unit |
| `tests/integration/test_rtl_to_verif.py` | R6 | integration |
