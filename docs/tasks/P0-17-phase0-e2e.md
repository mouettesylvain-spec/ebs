# P0-17 — Phase 0 end-to-end and examples

Status: todo · Phase: 0 · Depends on: P0-16 · Size: M

## Goal
Prove the phase 0 exit criterion — an existing lint Makefile wrapped as-is gets a 100 % cache hit on the
second run — and ship open-source examples outside contributors can run without vendor licenses.

## Read first
- docs/architecture.md § "Roadmap and tech stack" (phase 0 row, Open-source setup)

## Scope (files)
- create `examples/lint-make/` (a small SystemVerilog design + a Makefile running `verilator --lint-only`
  when available, else a fake lint script producing a normalized report, plus a downstream `summary`
  shell step that consumes the report)
- create `examples/verilator-sim/` (compile + matrix of tests with seeds using `shell` steps)
- create `tests/e2e/test_phase0_exit.py`, `docs/user/quickstart.md`

## Requirements
- R1 First `ebs build` of `examples/lint-make` runs the make step; second run with no changes reports
  100 % cached and submits nothing (assert on events).
- R2 A comment-only change in one `.sv` file reruns lint; because the normalized report is identical and
  deterministic, downstream steps hit the cache (early cutoff demonstrated).
- R3 A real content change reruns exactly the affected actions (assert the set of rerun action ids).
- R4 Editing a source during a running build doesn't affect that build (I7, via a slow fake lint).
- R5 Quickstart doc commands are executed by the test (extract fenced `console` blocks), so docs can't rot.
- R6 The examples run in CI in < 2 min with the fake tools and nightly with real Verilator.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_phase0_exit.py::test_second_run_fully_cached` | R1 | e2e |
| `::test_early_cutoff_comment_change` | R2 | e2e |
| `::test_minimal_rerun_set` | R3 | e2e |
| `::test_edit_during_build_isolated` | R4 | e2e |
| `tests/e2e/test_docs.py::test_quickstart_blocks` | R5 | e2e |

## Done when
- [ ] all e2e tests pass in CI; phase 0 exit criterion demonstrated in the MR description with output
