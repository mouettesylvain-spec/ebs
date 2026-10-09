# P0-17 — Phase 0 end-to-end and examples

Status: review · Phase: 0 · Depends on: P0-16 · Size: M

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
- [x] all e2e tests pass locally (`make check`, `make test-integration`); CI run pending on the PR
- [ ] phase 0 exit criterion demonstrated in the MR description with output (human, when opening the MR)

## Notes

### What was built (2026-10-09)
- `examples/lint-make`: an unchanged-style `lint/Makefile` run per row of `blocks.csv`
  (`kind: make`), `tools/fake-lint` (bash + awk: MODNAME, UNUSED, TAB, LONGLINE on comment-stripped
  code, normalized report), `tools/summarize`. `examples/verilator-sim`: `compile` (model dir
  `deterministic: false`), `sim` over `tests.csv`, `report`.
- Real Verilator is a separate `flow.verilator.yaml` per example declaring a `verilator/system`
  **toolchain**, not Makefile auto-detection: the tool's identity must be in the action key, or a
  fake-lint result would be a cache hit for a Verilator build on another machine.
- R6: the e2e tests take ~50 s locally (`tests/e2e`, including P0-16's); the < 2 min budget is not
  asserted (only the 120 s per-test timeout). Real Verilator runs in the new nightly `verilator`
  job (`EBS_E2E_VERILATOR=1`); it was then run locally with Verilator 5.020 (Ubuntu 24.04, the CI
  runner's version): `EBS_E2E_VERILATOR=1 uv run pytest tests/e2e` gives 11 passed. That first run
  found two bugs, both fixed: `VERILATOR_ROOT` in the toolchain env breaks distro packages (exit
  127), and the test signal `unused_dbg` was silently exempt under Verilator's default
  `--unused-regexp "*unused*"` (renamed `spare_dbg`; fake-lint now honors the same exemption).
- CI triggers now use `paths:` with negations so quickstart-only PRs still run `test_docs.py`.

### Review (task-reviewer)
No blockers. Fixed: a missing/crashing Verilator no longer yields an empty "0 violation(s)"
report (`|| true` removed) and the nightly test asserts a Verilator-only finding; R4 also edits a
file only the downstream step reads, so it catches a leak whatever order the lints stage in
(this exposed that `lint` declared `tools/*`, rerunning on summary-tool edits: now
`tools/fake-lint`); `paths-ignore` + `!` replaced by the documented `paths` form; the Makefile's
report is `.PHONY` so a stale local report is never trusted; nightly job no longer
`continue-on-error`. Accepted: the quickstart's `sed -i` edits need GNU sed (said in the doc).

### Follow-ups
- No CLI to migrate the metadata DB: the quickstart uses
  `python -c 'from ebs.meta import migrations; migrations.upgrade(...)'`. Add `ebs admin db upgrade`
  (P0-11 or P1-06).
- `verilator/system` fingerprints only `VERILATOR_ROOT`; the binary is identified by its version
  string. Proper registration is P1-07.
- Fixed in a separate commit on this branch: `test_planner.py::test_refine_idempotent` (perf tier,
  P0-08) failed ~1 in 7 runs because its 200 ms Hypothesis deadline also timed plan setup I/O
  (~80 ms, spikes to ~470 ms); setup now runs once outside the timed examples.
