# P3-05 — Revalidation jobs and comparators

Status: review · Phase: 3 · Depends on: P3-04 · Size: L

## Goal
Scheduled re-runs of archived sign-off plans on the newest tool versions, compared with per-step
comparators, producing a report attached to the release — reproducibility without archiving tool binaries.

## Read first
- docs/architecture.md § "Versioning…" → Long-term reproducibility item 2

## Scope (files)
- create `src/ebs/release/revalidate.py`, `src/ebs/rules/comparators/{api,sim,lint}.py`, CLI
  `ebs release revalidate`, scheduler unit `deploy/revalidate/*.timer`
- tests `tests/unit/release/test_revalidate.py`, `tests/unit/rules/comparators/*`

## Requirements
- R1 `revalidate RELEASE --toolchain-override questa=latest` re-plans from the bundle with overridden toolchains
  (recorded in the report) and runs as a normal build in a dedicated `revalidation` mode (cache write off).
- R2 Comparator protocol per rule kind: `compare(old_outputs, new_outputs, old_summary, new_summary) -> Verdict`
  (`equivalent | changed(details) | incomparable`). Sim: per (test, seed) pass/fail equality and coverage delta
  within tolerance; lint: violation set diff after normalization (rule id + file + line + message template).
- R3 Report (JSON + Markdown) stored in CAS, recorded in `revalidations`, attached to the release, summarized in
  the dashboard; overall verdict `reproduced | differences | failed`.
- R4 Quarterly schedule per domain (timer), with a notification hook (email/webhook config).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_revalidate.py::test_override_recorded_and_no_cache_write` | R1 | unit |
| `comparators/test_sim.py`, `test_lint.py` (table-driven) | R2 | unit |
| `test_revalidate.py::test_report_and_verdict` | R3 | unit |
| `tests/unit/deploy/test_timer_units.py` | R4 | unit |

Also: `test_revalidate.py::test_comparator_by_step` (R2+R3), `tests/unit/cli/
test_release_revalidate.py` (R1, R3 through the CLI), `tests/contract/test_release_store.py::
test_record_revalidation` (R3, memory + pg), `tests/integration/release/test_api.py::
test_record_revalidation` (R3 over HTTP), web/e2e/releases.spec.ts (dashboard column).

## Notes
- CONTRACT CHANGE: `ReleaseStore.record_revalidation` / `list_revalidations`,
  `RevalidationCreate`, `RevalidationRecord`, `RevalidationList`, `RevalidationVerdict`,
  `ComparisonKind`, routes `POST|GET /releases/{id}/revalidations` (`release_write` / read),
  table `revalidations` (revision 0016, append-only triggers; replaces the data-model sketch's
  `started_at/finished_at/toolchain_overrides` columns with the build, both report digests, the
  modules used and the counts). Report and Markdown digests are `GcRoots.released`, so a report
  lives as long as its release.
- Layering: `release` (L3) cannot import `driver` (L4), so `revalidate` takes a `BuildRunner`;
  `ebs.cli.revalidate` wires `run_build` (`keep_going=True`) on the chosen executor.
  `ebs.release.verify.plan_bundle` is the re-plan shared with `ebs verify`.
- "Revalidation mode" (R1) = cache mode `read` (default) or `off` (`--no-cache`); `write` is
  refused, so results of unproven tools never feed other builds. The build kind stays `dev`
  (no new SLURM QOS kind); the build is identified as a revalidation by its `revalidations` row.
- Override `NAME=latest` = the most recently registered, non-superseded toolchain of the same
  tool (`ToolchainRecord.name`, the module before `/`); `NAME=MODULE` = that module's current
  toolchain. Both are resolved before anything runs; the report records recorded/used module and
  id per toolchain, the row the module used.
- No lint rule kind exists (lint is wrapped with `make`): comparators are chosen per step name or
  rule kind in `[revalidate.comparators]` (default `questa.sim = "sim"`). Steps without one are
  compared by status and deterministic output digests; an action with only nondeterministic
  outputs and no comparator is `incomparable` and not counted in the verdict.
- Coverage: the sim comparator reads a `coverage` percentage from the result document or the
  summary; `questa.sim` does not produce one yet (needs `vcover report`), so coverage is only
  compared when a rule or wrapper reports it.
- R4 notification: webhook (https only) JSON POST and/or SMTP email, stdlib only (no new
  dependency); delivery failures are warnings. The unit runs `--all --executor slurm`, treats
  exit 1 (a release did not reproduce) as success, and needs `release_write` on the domain.
- `--all` keeps going past a release it cannot revalidate (warning, exit 1 at the end); overrides
  from `[revalidate].toolchain_overrides` are skipped for plans without that toolchain, while a
  `--toolchain-override` is strict. An error while comparing (after the build ran) is recorded as
  a `failed` revalidation with its error.
- `latest` orders by registration time among current toolchains, not by version string.
- `ebs.release.archive._restore_objects` became public `restore_objects` (revalidation reads the
  bundled outputs through it).
- Review: task-reviewer (3 SHOULD-FIX, all fixed: --all keep-going, nested module names for
  `latest`, comparison errors recorded) and test-critic (39 surviving mutants of 50; tests added
  for every one that could hide a wrong verdict: comparator + status change, action gone from the
  re-plan, recorded bytes from the bundle, skipped actions, superseded toolchains, config
  precedence and settings, nan/inf coverage, nondeterministic outputs, `when = "problems"`).
- The Playwright e2e suite could not run locally (the headless browser lacks `libnspr4.so`); CI
  runs it.
- Follow-ups: (1) a `questa.sim` coverage summary (`vcover report -summary`) so coverage deltas
  are real; (2) a `revalidation` SLURM QOS kind if sites want them below dev priority; (3) show
  the report itself in the dashboard (needs a CAS read route for report blobs); (4) a `lint` rule
  kind (SpyGlass) with a normalized report output; (5) the dashboard fetches revalidations per
  release row (N+1): add the latest one to `ReleaseView` or a newest-first `limit=1` query;
  (6) `plan_bundle`'s in-memory stat cache is never closed (ResourceWarning, also in P3-04's
  `replan`).
