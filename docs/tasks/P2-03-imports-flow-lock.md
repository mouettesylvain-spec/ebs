# P2-03 — Imports and `flow.lock`

Status: done · Phase: 2 · Depends on: P2-02 · Size: M

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

## Notes
- Dependency: P2-02 was merged to main but still marked `review`; implemented on top of it with
  the user's go-ahead.
- Layering: the planner (L2) cannot import `ebs.release` (L3), so it takes the pins as
  `ebs.plan.types.PlanLock` / `ImportPin`; `ebs.release.lock.plan_lock` builds them from
  `flow.lock`. `load_flow(lock=)` stays unused.
- Staging layout: `imports/<alias>/<release output name>` (release outputs are named
  `<action id>.<output>`, e.g. `pack.srcs`), so flows write `${imports.rtl}/pack.srcs/**/*.sv`.
  A release output named after a matrix action (`compile[lib=core].worklib`) has `[`, which is a
  glob class in a pattern; such an output is reachable whole (`${imports.x}/<exact name>`
  matches before globbing) or with `?` in place of the brackets.
- Ids: a file inside a released tree uses its manifest digest; a whole output uses the
  release's id (for `deterministic: false`, the producer-key id, not the bytes). `ImportInput`
  gained `content` (plan.json only, not in the key), so the runner needs no store lookup and an
  exported release (no provenance edges in the consumer domain) still stages.
- `release_digest` excludes the domain so an exported copy pins the same digest; the lock
  entry's `domain` must equal the flow's domain (else stale).
- `lock check` also fails on a layout-only difference (the file is generated; any drift means
  it was hand-edited).
- Follow-ups: `ebs used-by` (P2-04) reads `release_imports`; consider warning in `lock check`
  when a pinned release has been yanked since it was locked (the plan still builds with it).
- Pre-existing, unrelated: `tests/integration/slurm/test_slurm_executor.py::test_fake_regression_2000`
  times out (60 s wait) on this machine on untouched origin/main too; not caused by P2-03.
