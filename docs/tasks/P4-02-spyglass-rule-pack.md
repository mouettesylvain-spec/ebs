# P4-02 — SpyGlass rule pack and normalized violation reports

Status: todo · Phase: 4 · Depends on: P1-08, P3-05 · Parallel-safe with: P4-01, P4-04, P4-06, P4-07 · Size: M

## Goal
A `spyglass` step kind (VC SpyGlass lint and CDC goals) whose reports are normalized, so a comment-only RTL change
yields an identical report and stops propagating (early cutoff); plus reuse of the P3-05 lint comparator.

## Read first
- docs/architecture.md § "Hashing…" → Nondeterministic outputs (early cutoff); § "Flow description format"
  (`lint_legacy` example)
- docs/design/interfaces.md §7
- Existing code to follow: `src/ebs/rules/questa/`, `src/ebs/rules/comparators/lint.py`

## Scope (files)
- create `src/ebs/rules/spyglass/{__init__,rule,tcl,report}.py`
- create `tests/fakes/bin/spyglass` (Python), `tests/unit/rules/spyglass/*`, `tests/vendor/spyglass/*`
- modify `src/ebs/rules/comparators/lint.py` only to accept the normalized record shape if it differs

## Out of scope
Waiver management UI, QoR trending, other Synopsys lint tools. Comparator protocol changes (P3-05 owns it).

## Requirements
- R1 `spyglass`: generates the project Tcl through the P0-12 params-file helper (goals, top, `lint_opts`), runs
  the tool with argv lists only, outputs `reports` (dir, deterministic) and `violations.json`.
- R2 Report normalization (`report.py`): parse the violation report into records (rule id, file, line, message
  template with numbers/paths normalized), sort stably, drop timestamps, hostnames and run ids; the normalized
  JSON is the deterministic output so its digest is the early-cutoff id.
- R3 Normalization is idempotent and order-insensitive: shuffling or re-timestamping a report yields the same
  digest; adding or removing one violation changes it.
- R4 `classify`: license failure ⇒ INFRA("license"); crash ⇒ INFRA("tool_crash"); violations above the
  configured severity threshold ⇒ FAILED; otherwise PASSED.
- R5 `violations.json` is accepted by the P3-05 lint comparator unchanged (diff by rule id + file + line +
  message template).
- R6 Rule version and golden argv/Tcl tests.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_rule.py::test_argv_golden`, `::test_tcl_golden` | R1, R6 | unit |
| `test_report.py::test_parse_fixtures[...]` | R2 | unit |
| `test_report.py::test_normalization_idempotent_and_order_insensitive` (Hypothesis) | R3 | property |
| `test_rule.py::test_classify[...]` | R4 | unit |
| `tests/unit/rules/comparators/test_lint.py::test_spyglass_records` | R5 | unit |
| `tests/integration/test_spyglass_early_cutoff.py` (comment-only change ⇒ downstream cached) | R2, R3 | integration |
| `tests/vendor/spyglass/test_real_spyglass.py` | R1, R4 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Synthetic report fixtures only. Normalization drops information on purpose; keep the raw report as a separate
(nondeterministic-safe) output for humans.
