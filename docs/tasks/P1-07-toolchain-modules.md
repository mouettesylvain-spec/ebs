# P1-07 — Toolchain registration via modulefiles

Status: review · Phase: 1 · Depends on: P0-07, P0-10 · Size: M

## Goal
`ebs toolchain register questa/2025.2` snapshots a module (env, install roots, fingerprint) into an
immutable registered toolchain that flows reference by module name.

## Read first
- docs/architecture.md § "Hashing…" → "Toolchains"; docs/design/data-model.md `toolchains`

## Scope (files)
- create `src/ebs/toolchain/modules.py` (Environment Modules + Lmod detection), `registry.py`
  (`RegistryToolchainResolver`), `src/ebs/cli/toolchain.py` (`register`, `list`, `show`, `check`)
- tests `tests/unit/toolchain/test_modules.py` (fake `modulecmd` script), `test_registry.py`

## Requirements
- R1 `register MODULE` runs `module purge; module load MODULE` in a clean shell (P0-07 `capture_env`), records
  the env diff, derives install roots from env vars pointing into the module tree plus explicit
  `--root` options (e.g. UVM, VIP dirs), fingerprints them, and stores the toolchain row.
- R2 Registered toolchains are immutable. Registering again with an identical id is a no-op; with a different
  fingerprint it creates a new id, marks the old one `superseded_by`, and prints a drift warning listing the
  changed files (first 20).
- R3 `RegistryToolchainResolver.resolve(name, module)` returns the latest non-superseded id for the module;
  the plan records the id, so old builds keep pointing at the old snapshot.
- R4 `ebs toolchain check` re-fingerprints all registered toolchains used in the last N days and reports drift
  (nightly job); it never changes existing ids.
- R5 Works with Environment Modules 4/5 and Lmod (detect via `$MODULESHOME` / `module --version`); tested
  against fake `modulecmd`/`lmod` scripts that emit shell code like the real ones.
- R6 Toolchain env never includes the registering user's HOME or username (normalized, as in P0-07 R4).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_modules.py::test_register_captures_env_and_roots` | R1 | unit |
| `test_registry.py::test_idempotent`, `::test_drift_new_id_and_warning` | R2 | unit |
| `test_registry.py::test_resolve_latest`, `::test_old_plan_keeps_id` | R3 | unit |
| `test_registry.py::test_check_reports_drift_only` | R4 | unit |
| `test_modules.py::test_flavours[envmodules,lmod]` | R5 | unit |
| `test_modules.py::test_user_independent` | R6 | unit |
| `tests/vendor/test_register_questa.py` (real module on grid) | R1 | vendor |

## Done when
- [x] `make check-all` passes (integration run against a local PostgreSQL 16 via `EBS_TEST_PG_URL`)

## Notes
- CONTRACT CHANGE: `MetadataStore` gains `register_toolchain`, `get_toolchain`,
  `current_toolchain`, `list_toolchains`, `toolchain_manifest`, `touch_toolchain` (models
  `ToolchainCreate`, `ToolchainRecord`, `RegisterOutcome`); migration `0004` adds
  `toolchains.last_used_at` (R4 "used in the last N days") and `toolchain_manifests` (zlib
  fingerprint lines, R2's changed-file list). New `ebs.toolchain.fingerprint.fingerprint_entries`.
- Stores do not recompute toolchain ids: the runner imports the meta store and import-linter
  forbids it from importing `ebs.toolchain`. The registry computes ids on write and re-checks
  them on every read (`to_toolchain`).
- The stored env is the diff against the same clean shell without the load. A modulefile that
  *unsets* a baseline variable cannot be represented (debug log). License variables
  (`DEFAULT_PASSTHROUGH_ENV`) are dropped: they are site facts the runner passes through.
- Registering a superseded id again (reverted install) reinstates it as current; status
  `reinstated`.
- `check` covers current toolchains whose `last_used_at`, else `registered_at`, is within
  `--days`; it exits 1 on drift or an unreadable tree.
- Module names must carry an explicit version (`name/version`): a default version can change
  under the same name.
- Follow-ups: `check` re-fingerprints install trees only; it could also re-capture the module
  env to catch modulefile edits (needs the module system on the checking host). Site-specific
  Lmod settings (`LMOD_*` config variables) are not passed into the capture shell yet; add a
  `[toolchain].capture_env` passthrough list if a site needs them. `register` uses the default
  passthrough list rather than the site's `[runner].passthrough_env`.
