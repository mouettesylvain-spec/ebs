# P1-08 — Questa rule pack and fake Questa tools

Status: review · Phase: 1 · Depends on: P0-12 · Size: L

## Goal
First-class `questa.compile`, `questa.opt` and `questa.sim` step kinds that generate correct command
lines, handle work libraries and `modelsim.ini`, and classify results (UVM errors, license failures).

## Read first
- docs/architecture.md § "Flow description format" (YAML example: compile/elab/sim), § "Hashing…" →
  Nondeterministic outputs; § "Risks" (compile granularity per library)
- docs/design/interfaces.md §7; docs/design/testing.md → Fake tools

## Scope (files)
- create `src/ebs/rules/questa/{__init__,compile,opt,sim,ini,transcript}.py`
- create `tests/fakes/bin/{vlib,vmap,vlog,vcom,vopt,vsim}` (Python), `tests/unit/rules/questa/*`,
  `tests/vendor/questa/test_real_questa.py`

## Out of scope
Coverage merging (`vcover merge`) — follow-up task after P1-12. GUI (`ebs reproduce --interactive` handles it).

## Requirements
- R1 `questa.compile`: per library `vlib work/<lib>`, then `vlog`/`vcom` (by file extension or `params.lang`)
  with `-work work/<lib>`, the filelist (`-f`) and user `vlog_opts` split with shlex; generated
  `modelsim.ini` maps every upstream library (fan-in inputs) to its logical path; output `worklib` is a dir,
  `deterministic: false` by default.
- R2 `questa.opt`: `vopt` with `-L` for each input lib, `top`, `-o opt`; output `model` dir (nondeterministic).
- R3 `questa.sim`: `vsim -c -batch` with `-sv_seed <seed>`, `+UVM_TESTNAME=<test>`, plusargs from params
  (split safely), `-do` script generated (`run -all; quit -f`), optional coverage `-coverage` + `coverage save`
  when output `cov` requested; writes `result.json` from the transcript.
- R4 Transcript parsing (`transcript.py`): UVM report summary counts (`UVM_ERROR`, `UVM_FATAL`), `** Error`/
  `** Fatal` lines, `$finish`/`$fatal` markers, first error line (signature for dashboard clustering, with
  numbers/hex/paths normalized). PASSED iff exit 0, no fatals, UVM_ERROR = 0, and a completion marker exists.
- R5 License failures (checkout errors, "Unable to checkout") ⇒ INFRA("license"); crash signals ⇒
  INFRA("tool_crash"); everything else non-passing ⇒ FAILED.
- R6 Rule versions and golden argv tests for each kind (as P0-12 R6).
- R7 Fake tools reproduce: output dirs with embedded timestamps (so reruns differ in bytes), transcript formats,
  modes selected by env (`FAKE_QUESTA_MODE=pass|uvm_error|fatal|license_fail|segv|hang`).
- R8 Vendor test: a tiny SystemVerilog UVM-free testbench compiles, optimizes and simulates with real Questa
  and passes; a deliberately failing test is FAILED with the right first-error signature.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_compile.py::test_argv_golden[sv,vhdl]`, `::test_modelsim_ini_maps_fanin` | R1 | unit |
| `test_opt.py::test_argv_golden` | R2 | unit |
| `test_sim.py::test_argv_golden`, `::test_plusargs_split_safely` (Hypothesis) | R3 | unit/property |
| `test_transcript.py::test_parse_fixtures[...]`, `::test_signature_normalization` | R4 | unit |
| `test_sim.py::test_classify[...]` | R5 | unit |
| `test_*::test_rule_version_golden` | R6 | unit |
| `tests/integration/test_questa_fake_flow.py` (compile→opt→sim through driver + local executor) | R1–R5, R7 | integration |
| `tests/vendor/questa/test_real_questa.py` | R8 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)

## Notes
Transcript fixture files must be synthetic (hand-written in the shape of real output), not copied from
vendor documentation or real project logs.

Implementation notes (P1-08):
- `result.json` is written in two stages because the runner calls `classify` before it collects
  outputs, and only with outputs that exist: the generated `.ebs/questa_sim.sh` writes a stub
  (test, seed, the result's logical path) and then `exec vsim`; `QuestaSimRule.classify` (runner
  side) parses `<work>/transcript` (or the log tail if there is none) and replaces the stub with
  the final, timestamp-free document. So no Python is needed on the tool's PATH and the runner is
  unchanged. A stub whose recorded path does not match where it lies is left alone.
- Fan-in library names: the planner stages upstream outputs at `<output>/<producer action id>`,
  so a library's logical name is the producer's `lib` matrix value (`compile[lib=core]` → `core`),
  or its step name without a matrix. `questa.compile` rejects a `lib` param that differs from that
  name, so producers and consumers always agree (needed for VHDL `library` clauses and recorded
  dependencies). modelsim.ini is generated at the work-dir root (`-modelsimini modelsim.ini`), not
  under `.ebs/`, so relative library paths mean the same whether the tools resolve them against
  the ini's directory or the cwd; `others = $MODEL_TECH/../modelsim.ini` adds the standard libs.
- A sim step must get the libraries its model was optimized from (as `libs:`) besides `model`;
  fake vsim enforces this, and the integration flow does it.
- Bash exit 126/127 (tool not on PATH) is INFRA("tool_missing") for all three kinds, like a
  runner that cannot start the tool; it is never a cached FAILED.
- Classifying an already finalized `result.json` again returns the same answer (idempotent).
- Documented in interfaces.md § 7 (CONTRACT CHANGE, documentation of existing runner behaviour):
  the runner calls `classify` once, before collecting outputs, and a rule may finalize outputs
  there; pinned by tests/unit/runner/test_classify_order.py.
- Generated outputs when the flow does not declare them: `worklib: work/<lib>` and `model: opt`
  (both `deterministic: false`), `result: result.json`. A declared `worklib`/`model` must set
  `deterministic` explicitly. Coverage is enabled only when output `cov` is declared
  (`coverage save -onexit <cov>` in the do file, so it is saved even when `$finish` ends the run).
- `seed` is required and must be a non-negative integer (explicitness rule 1); plusargs must
  start with `+` (options go in `vsim_opts`), which also blocks injecting `-do`.
- R4 is implemented literally: a bare `** Error` (e.g. SVA `$error`) is counted in result.json
  but does not fail a test. A license message only makes a run INFRA if it did not reach
  `$finish`; the tools' own `** Fatal: (SIGSEGV)` reports are INFRA("tool_crash") for all three
  kinds.
- New shared module `src/ebs/rules/questa/_common.py` (script, params, outputs, base
  classification), not in the original file list.
- The integration test uses `ebs build` for pass/fail/caching and in-process `run_build` (CLI
  planner, local executor, real `ebs-runner`) with a zero-delay retry policy for license, crash
  and hang. Locally it ran on PostgreSQL 16 via `EBS_TEST_PG_URL` (no Docker daemon).

Follow-ups:
- R8 vendor test not yet run on a Questa node (no licenses here); the "Done when" nightly link is
  still open. Things to confirm there: that modelsim.ini and the tools accept the staged paths
  (`worklib/c[lib=rtl]`: brackets and `=`; the vendor test uses them) and whether vsim really needs
  the source libraries next to an optimized model.
- Optional strict mode (e.g. param `fail_on_error: true`) to fail tests on any `** Error`.
- Coverage merging (`vcover merge`) as planned, after P1-12.
