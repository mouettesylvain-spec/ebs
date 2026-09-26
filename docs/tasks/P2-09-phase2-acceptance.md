# P2-09 — Phase 2 acceptance

Status: todo · Phase: 2 · Depends on: all P2 tasks · Size: S

## Goal
Demonstrate: RTL publishes a release that verification consumes through its lockfile, and
`ebs tests <release>` answers correctly.

## Requirements
- R1 Grid scenario: RTL team flow builds and releases `rtl-team/cpu <version>` to `stable`; verification flow runs
  `ebs lock update`, commits the lock, builds via CI; results visible in the MR (JUnit) and dashboard.
- R2 `ebs tests rtl-team/cpu <version>` lists exactly the sim actions of that verification build (and later builds
  using the same release), with correct status/seed/toolchain.
- R3 A user outside the RTL domain cannot see any of it through CLI, API or dashboard.
- R4 Report `docs/ops/phase2-report.md` with screenshots and command transcripts.

## Tests
`tests/grid/test_phase2_exit.py::{test_release_consumed_via_lock, test_tests_query, test_domain_isolation}` (grid)
