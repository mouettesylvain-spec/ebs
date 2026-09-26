# P3-09 — Phase 3 acceptance

Status: todo · Phase: 3 · Depends on: all P3 tasks · Size: S

## Requirements
- R1 The sandbox is on by default for Questa kinds; the full regression passes under it on the grid.
- R2 A quarterly revalidation of a signed-off release runs with the newest Questa and produces a report attached
  to the release; the report is reviewed by the verification lead.
- R3 `ebs verify` passes on a restored sign-off bundle in an empty environment.
- R4 Report `docs/ops/phase3-report.md`.

## Tests
`tests/grid/test_phase3_exit.py::{test_sandboxed_regression, test_revalidation_report, test_bundle_restore_verify}`
