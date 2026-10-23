# P4-01 — VCS rule pack (`vcs.compile`, `vcs.sim`)

Status: todo · Phase: 4 · Depends on: P1-08 · Parallel-safe with: P4-02, P4-04, P4-06, P4-07 · Size: L

## Goal
First-class `vcs.compile` and `vcs.sim` step kinds (vlogan/vcs/simv) that generate correct command lines,
treat `simv.daidir` as a nondeterministic output, and classify results like the Questa pack.

## Read first
- docs/architecture.md § "Hashing…" → Nondeterministic outputs; § "Flow description format"
- docs/design/interfaces.md §7
- Existing code to follow: `src/ebs/rules/questa/` (layout, `transcript.py`), `tests/fakes/bin/`

## Scope (files)
- create `src/ebs/rules/vcs/{__init__,compile,sim,synopsys_setup,log}.py`
- create `tests/fakes/bin/{vlogan,vhdlan,vcs,simv}` (Python), `tests/unit/rules/vcs/*`,
  `tests/vendor/vcs/test_real_vcs.py`
- modify the rule registry entry points only to register `vcs.*`

## Out of scope
Coverage merging (`urg`/`vcover`), Verdi/FSDB waveform handling, GUI debug, UVM-free-only simplifications.
Questa behaviour changes (P1-08 owns it).

## Contract
Same `Rule` protocol as P1-08 (interfaces.md §7); no new public types. Rule ids `vcs.compile`, `vcs.sim`.

## Requirements
- R1 `vcs.compile`: per library `vlogan -work <lib>` (or `vhdlan` by extension / `params.lang`) with the filelist
  (`-f`) and `vlog_opts` split with shlex, then `vcs` elaboration with `-ntb_opts uvm` when `params.uvm` is true
  and the top from params; a generated `synopsys_sim.setup` maps every fan-in library to its logical path.
  Outputs `simv` (file) and `simv.daidir` (dir, `deterministic: false`).
- R2 `vcs.sim`: run `simv` with `+ntb_random_seed=<seed>`, `+UVM_TESTNAME=<test>`, plusargs split safely, `-l`
  to a log; writes `result.json` from the log.
- R3 Log parsing: UVM report summary counts, `Error-[...]` / `Fatal` lines, `$finish` marker, first error line
  normalized (numbers, hex, paths) for dashboard clustering. PASSED iff exit 0, no fatals, UVM_ERROR = 0 and a
  completion marker exists.
- R4 `classify`: license checkout failures ⇒ INFRA("license"); crash signals ⇒ INFRA("tool_crash"); anything
  else non-passing ⇒ FAILED.
- R5 Rule versions and golden argv tests per kind (as P0-12 R6).
- R6 Fake tools reproduce timestamped `simv.daidir` contents (so reruns differ in bytes), log formats, and modes
  via `FAKE_VCS_MODE=pass|uvm_error|fatal|license_fail|segv|hang`.
- R7 Vendor test: a tiny UVM-free testbench compiles and simulates; a failing test is FAILED with the right
  first-error signature.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_compile.py::test_argv_golden[sv,vhdl]`, `::test_setup_maps_fanin` | R1 | unit |
| `test_sim.py::test_argv_golden`, `::test_plusargs_split_safely` (Hypothesis) | R2 | unit/property |
| `test_log.py::test_parse_fixtures[...]`, `::test_signature_normalization` | R3 | unit |
| `test_sim.py::test_classify[...]` | R4 | unit |
| `test_*::test_rule_version_golden` | R5 | unit |
| `tests/integration/test_vcs_fake_flow.py` (compile→sim through driver + local executor) | R1–R4, R6 | integration |
| `tests/vendor/vcs/test_real_vcs.py` | R7 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Log fixtures must be synthetic (hand-written in the shape of real output), never copied from vendor
documentation or real project logs. No license server names or hostnames in fixtures.
