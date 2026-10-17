# P1-12 — Phase 1 acceptance on the grid

Status: todo · Phase: 1 · Depends on: all P1 tasks · Size: M

## Goal
Demonstrate the phase 1 exit criterion on the real cluster and make it a recurring nightly check.

## Read first
- docs/architecture.md § "Roadmap and tech stack" (phase 1 row), § "Risks" (NFS load, submission limits)
- docs/design/testing.md § CI pipeline; `.github/workflows/nightly.yml` (CI is GitHub Actions, decision D3)
- `tests/grid/test_slurm_real.py` (existing grid test: `EBS_GRID_PARTITION`, `EBS_GRID_ACCOUNT`,
  `EBS_GRID_QOS`, shared `--basetemp`); reuse its conventions

## Prerequisites (site, outside the repo)
- Site config with real `[slurm]` values (D5: limits, partition), `account_map`/`qos` (P1-03), the license
  server name, and a registered Questa toolchain (P1-07). It stays in site config, never in the repo.
- Self-hosted GitHub Actions runners labelled `slurm` (login node with the SLURM client) and
  `eda-licensed` (Questa licenses), as `nightly.yml` expects.

## Scope (files)
- create `tests/grid/test_phase1_exit.py`, `tests/grid/README.md`, `scripts/grid_metrics.py`
- `.github/workflows/nightly.yml`: enable the `schedule:` (resolve its `TODO(P1-12)`), run `grid` and
  `vendor` on the self-hosted runners, and move mutmut's paths to `[tool.mutmut]` in `pyproject.toml`
  (the other `TODO(P1-12)` there)
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

## Notes
- The report and the JSON artifact are committed to an open-source repo: no real hostnames, node names,
  partition/account names, license server names or design names (invariants). Use placeholders.
