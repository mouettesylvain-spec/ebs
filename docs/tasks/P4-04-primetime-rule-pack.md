# P4-04 — PrimeTime STA rule pack with per-corner matrix

Status: todo · Phase: 4 · Depends on: P0-05, P0-12, P1-08 · Parallel-safe with: P4-01, P4-02, P4-06, P4-07 · Size: M

## Goal
A `primetime` step kind that runs static timing analysis once per corner (a matrix over a corners table), so
each corner is its own cached action consuming a synthesized netlist.

## Read first
- docs/architecture.md § "Flow description format" → Matrix; § "Roadmap" phase 4 (synthesis → STA cached end to end)
- docs/design/interfaces.md §3 (matrix), §7 (rules)
- Existing code to follow: `src/ebs/rules/tcl.py`, `src/ebs/flow/` matrix expansion

## Scope (files)
- create `src/ebs/rules/primetime/{__init__,rule,tcl,report}.py`
- create `tests/fakes/bin/pt_shell` (Python), `tests/unit/rules/primetime/*`, `tests/vendor/primetime/*`,
  `examples/sta-corners/corners.csv` (synthetic)

## Out of scope
Synthesis (P4-03), signoff ECO loops, parasitic extraction, QoR trending, formal equivalence (P4-05).

## Requirements
- R1 `primetime`: one action per row of the corners table (`corner`, `lib_set`, `parasitics`, `mode`); Tcl
  generated through the params-file helper with the row's values; argv lists only.
- R2 Corner is part of the action key through params; changing one corner's row reruns only that corner.
- R3 Outputs `timing.rpt`, `violations.json` (normalized: path group, endpoint, slack bucket) and
  `summary.json` (WNS/TNS/violating paths) — all deterministic.
- R4 Report parsing: WNS, TNS, violating path count from fixture reports; malformed report ⇒
  `EbsError` naming the file and expected section.
- R5 PASSED iff exit 0 and WNS ≥ 0 (threshold configurable via param `min_slack`); timing violations are
  FAILED (a valid, cached result), license/crash ⇒ INFRA as in P1-08.
- R6 Rule version and golden argv/Tcl tests.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_rule.py::test_argv_golden`, `::test_tcl_golden_per_corner` | R1, R6 | unit |
| `test_matrix.py::test_one_corner_change_reruns_one_action` | R2 | unit |
| `test_rule.py::test_declared_outputs` | R3 | unit |
| `test_report.py::test_parse_fixtures[...]`, `::test_malformed_report_error` | R4 | unit |
| `test_rule.py::test_classify[...]` | R5 | unit |
| `tests/integration/test_sta_fake_flow.py` | R1–R5 | integration |
| `tests/vendor/primetime/test_real_primetime.py` | R1, R5 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Corner names and libraries in fixtures are invented (`ss_0p9v_125c` style placeholders without real library names).
