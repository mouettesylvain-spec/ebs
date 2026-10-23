# P4-05 — Formality equivalence as a revalidation comparator

Status: todo · Phase: 4 · Depends on: P3-05, P4-03 · Parallel-safe with: P4-01, P4-02, P4-04, P4-06, P4-07 · Size: M

## Goal
Netlists produced by revalidation runs are compared with the archived ones by formal equivalence (Formality), so
the report says "equivalent" for logically identical netlists even when bytes differ.

## Read first
- docs/architecture.md § "Versioning…" → Long-term reproducibility item 2 (comparators; formal equivalence for
  netlists later)
- docs/tasks/P3-05-revalidation.md (comparator protocol, verdicts)
- Existing code to follow: `src/ebs/rules/comparators/{api,sim,lint}.py`

## Scope (files)
- create `src/ebs/rules/comparators/formality.py`, `src/ebs/rules/formality/{__init__,tcl,log}.py`
- create `tests/fakes/bin/fm_shell` (Python), `tests/unit/rules/comparators/test_formality.py`,
  `tests/vendor/formality/*`
- modify the comparator registry only to register the netlist comparator

## Out of scope
Changing the comparator protocol or verdict set (P3-05 owns them), running Formality as an ordinary build step,
ECO/debug flows.

## Requirements
- R1 `compare(old_outputs, new_outputs, old_summary, new_summary) -> Verdict` for steps declaring a netlist
  output: runs `fm_shell` with a generated Tcl (reference = old netlist, implementation = new), argv lists only.
- R2 Verdict mapping: verified ⇒ `equivalent`; failing points ⇒ `changed(details)` with failing-point count and
  first failing points; tool crash, license failure, inconclusive/aborted ⇒ `incomparable` with the reason.
- R3 Byte-identical netlists short-circuit to `equivalent` without invoking the tool.
- R4 Missing netlist on either side ⇒ `incomparable("missing_output")`; never raises for data problems.
- R5 The Formality run is recorded in the revalidation report (tool version, log digest) and its CAS inputs are
  digest-verified before the run.
- R6 Comparator version participates in the report so a changed comparator is visible in later diffs.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_formality.py::test_argv_and_tcl_golden` | R1 | unit |
| `test_formality.py::test_verdict_mapping[verified,failed,inconclusive,license,crash]` | R2 | unit |
| `test_formality.py::test_identical_bytes_skip_tool` | R3 | unit |
| `test_formality.py::test_missing_output_incomparable` | R4 | unit |
| `test_formality.py::test_report_records_tool_and_log_digest`, `::test_input_digest_verified` | R5 | unit |
| `test_formality.py::test_comparator_version_in_report` | R6 | unit |
| `tests/vendor/formality/test_real_formality.py` | R1, R2 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
Depends on P4-03 only for realistic netlist outputs in the integration fixture; the comparator itself needs just
P3-05. Fixtures are tiny synthetic netlists.
