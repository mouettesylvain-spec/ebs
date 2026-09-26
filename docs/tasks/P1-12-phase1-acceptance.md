# P1-12 — Phase 1 acceptance on the grid

Status: todo · Phase: 1 · Depends on: all P1 tasks · Size: M

## Goal
Demonstrate the phase 1 exit criterion on the real cluster and make it a recurring nightly check.

## Read first
- docs/architecture.md § "Roadmap and tech stack" (phase 1 row), § "Risks" (NFS load, submission limits)

## Scope (files)
- create `tests/grid/test_phase1_exit.py`, `tests/grid/README.md`, `scripts/grid_metrics.py`
- enable nightly `grid` and `vendor` jobs in `.gitlab-ci.yml`
- write `docs/ops/phase1-report.md` with measured numbers

## Requirements
- R1 The full 32-core CPU regression (1,000+ tests) runs through `ebs build` on SLURM with Questa and
  completes; results match the team's current Makefile flow (same pass/fail per test/seed) on the same RTL commit.
- R2 A one-file RTL change recompiles only the libraries whose inputs changed (assert the set of rerun
  compile actions == expected libs from `libs.csv`), then reruns elab + sims (nondeterministic model).
- R3 A rerun with no changes submits zero jobs.
- R4 After the build (including a run cancelled mid-way), no `$TMPDIR/ebs` directories remain on the nodes
  used (checked with a follow-up array job over the same nodes).
- R5 Metrics recorded: planning time, cache-lookup time, submission count, peak pending jobs, NFS bytes
  written, CAS growth, median queue wait split by pending reason. Recorded in the report and as a JSON artifact.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_phase1_exit.py::test_full_regression_matches_make` | R1 | grid+vendor |
| `::test_one_file_change_minimal_recompile` | R2 | grid+vendor |
| `::test_noop_rerun_zero_jobs` | R3 | grid |
| `::test_no_orphan_scratch` | R4 | grid |
| `scripts/grid_metrics.py` output attached | R5 | evidence |

## Done when
- [ ] human reviews the report; phase 1 marked done in docs/tasks/README.md
