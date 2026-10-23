# P4-03 — Synthesis rule pack (`dc`, `fusion_compiler`)

Status: todo · Phase: 4 · Depends on: P0-12, P0-09, P1-08 · Parallel-safe with: P4-01, P4-02, P4-06, P4-07 · Size: L

## Goal
`dc.synth` and `fusion_compiler.synth` step kinds whose very large outputs (100+ GB databases) are stored and
consumed as trees without ever being copied just to be read.

## Read first
- docs/architecture.md § "Artifact storage and garbage collection" → Read path; § "Roadmap" phase 4
- docs/design/interfaces.md §5 (CAS), §7 (rules), §9 (runner materialization)
- Existing code to follow: `src/ebs/rules/tcl.py` (Tcl params file), `src/ebs/cas/materialize.py`

## Scope (files)
- create `src/ebs/rules/synth/{__init__,dc,fusion,tcl,log}.py`
- create `tests/fakes/bin/{dc_shell,fc_shell}` (Python), `tests/unit/rules/synth/*`,
  `tests/vendor/synth/*`

## Out of scope
QoR tracking and trending (non-goal for v1), PDK content (lives in its own domain, imported), floorplan/P&R
steps, chunked dedup (P4-06).

## Requirements
- R1 `dc.synth` / `fusion_compiler.synth`: tool invoked with argv lists; the flow's Tcl and variables are passed
  through the P0-12 params file (never string-interpolated into a shell); declared `env` only.
- R2 Outputs `netlist`, `sdc`, `reports` (deterministic) and `db` (tree, `deterministic: false` by default,
  size unbounded).
- R3 Large outputs: trees over the copy threshold are bind-mounted/symlinked read-only for consumers and never
  copied; a test with a sparse synthetic 5 GB file proves no byte copy occurs.
- R4 Log parsing: error/warning counts, first error line normalized for clustering; PASSED iff exit 0 and no
  `Error:` lines.
- R5 `classify`: license failure ⇒ INFRA("license"); OOM kill ⇒ INFRA("oom") (retry with more memory per
  P1-05); crash ⇒ INFRA("tool_crash"); otherwise FAILED.
- R6 PDK/library inputs are declared imports only; the plan rejects absolute paths into undeclared PDK trees.
- R7 Rule versions and golden argv/Tcl tests.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_dc.py::test_argv_golden`, `test_fusion.py::test_argv_golden`, `::test_tcl_no_interpolation` | R1, R7 | unit |
| `test_outputs.py::test_declared_outputs_and_nondeterministic_db` | R2 | unit |
| `test_materialize_large.py::test_sparse_5gb_tree_not_copied` | R3 | unit |
| `test_log.py::test_parse_fixtures[...]` | R4 | unit |
| `test_dc.py::test_classify[...]` (incl. OOM) | R5 | unit |
| `test_plan_pdk.py::test_undeclared_pdk_path_rejected` | R6 | unit |
| `tests/integration/test_synth_fake_flow.py` | R1–R5 | integration |
| `tests/vendor/synth/test_real_synth.py` | R1, R4 | vendor |

## Done when
- [ ] `make check-all` passes; vendor test passes on the grid (paste nightly job link)
- [ ] every R has a test; reviewer subagent reports no correctness gaps
- [ ] Status set to `review` here and in docs/tasks/README.md

## Notes
No vendor data, PDK content or real library names in fixtures. Sparse files keep the large-output test fast and
disk-light; keep it under the `unit` 1 s budget or mark it `slow`.
