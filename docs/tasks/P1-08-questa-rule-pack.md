# P1-08 — Questa rule pack and fake Questa tools

Status: todo · Phase: 1 · Depends on: P0-12 · Size: L

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
