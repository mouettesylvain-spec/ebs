# P1-07 — Toolchain registration via modulefiles

Status: todo · Phase: 1 · Depends on: P0-07, P0-10 · Size: M

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
- [ ] `make check-all` passes
