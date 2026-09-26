# P2-08 — `--detach` driver job

Status: todo · Phase: 2 · Depends on: P1-02 · Size: M

## Goal
Long regressions survive logout: `ebs build --detach` hands the driver to a small SLURM job, and any driver
can re-attach to a build after a crash.

## Read first
- docs/architecture.md § "System architecture" (Driver row)

## Scope (files)
- modify `src/ebs/cli/build.py`, `src/ebs/driver/build.py`; create `src/ebs/driver/resume.py`
- tests `tests/unit/driver/test_resume.py`, `tests/integration/slurm/test_detach.py`

## Requirements
- R1 `--detach` submits `ebs build --resume-into <uuid>` as a 1-CPU job (partition/QOS from config, walltime
  from config), prints the build UUID and job id, and exits 0; `ebs status`/`logs` work as usual.
- R2 `--resume <uuid>` reloads the plan from CAS, reconciles action states from MetadataStore and the executor
  (re-attaching to recorded SLURM job ids), and continues without resubmitting running/finished actions.
- R3 Only one live driver per build (lease row with heartbeat); a second driver refuses unless the lease expired.
- R4 Cancelling the driver job cancels the build's jobs (driver SIGTERM handler → executor cancel).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_detach.py::test_detach_submits_driver_job` (FakeSlurm) | R1 | integration |
| `test_resume.py::test_reconcile_states[...]`, `test_detach.py::test_kill_driver_then_resume` | R2 | unit/integration |
| `test_resume.py::test_single_driver_lease` | R3 | unit |
| `test_detach.py::test_cancel_driver_cancels_jobs` | R4 | integration |
