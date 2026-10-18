# P2-08 — `--detach` driver job

Status: review · Phase: 2 · Depends on: P1-02 · Size: M

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

## Notes
- CONTRACT CHANGE: `MetadataStore.claim_driver` / `release_driver` (+ `DriverLease`,
  `DriverClaim`, `DRIVER_LEASE_TTL_S = 300`), table `driver_leases` (revision
  `0011_driver_leases`, after main's 0007-0010), routes `POST /builds/{b}/driver[/release]`, event type `build_resumed`,
  error `DriverBusyError` (exit 2). Fresh builds claim the lease too, so `--resume` is refused
  while the original `ebs build` lives. Extra tests: contract `test_driver_lease` (memory, pg,
  http, http-pg), `test_resume.py::test_running_build_holds_lease`,
  `::test_lost_lease_stops_driver_without_cancelling`, `::test_resume_finished_build_refused`.
- `--detach` starts the build in the CLI (record with `host: ""`, `pid: 0`, so `ebs status` does
  not call it interrupted) and the driver job *resumes into* it: R1 and R2 share one code path.
  The UUID is resolved through the local build record, so the driver job runs in the same
  (shared) directory (`--chdir`). Config: new `[driver]` section (`partition`, `qos`, `time`,
  default 72h); the account comes from `[slurm.account_map]`.
- Wired `--executor slurm` into the CLI (`SiteServices.open_executor`, P1-02 follow-up): a
  detached driver is only useful if its actions run on the grid.
- `Planner.refine` is now the module function `refine_plan` (it never needed the planner's
  collaborators), so a resumed driver refines without re-planning.
- Not persisted across a driver change: attempt counts, raised memory after an OOM and avoided
  nodes (they start afresh). A job submitted by sbatch whose id was not yet recorded when the
  driver died is invisible: its action is still `queued` and runs again (the cache's
  insert-if-absent keeps that harmless).
- Review fixes: SIGTERM is also tested on the resumed path (`test_cancel_resumed_driver_cancels_jobs`);
  lease renewal past 3 TTLs (`test_live_driver_renews_lease`); restored outputs join the new
  driver's GC lease (`test_restored_outputs_leased`); `--detach` requires `--executor slurm`;
  resuming SLURM jobs with an executor that cannot adopt them is refused
  (`test_resume_refuses_executor_that_cannot_attach`); SLURM action jobs also get
  `EBS_META_TOKEN` (`test_slurm_executor_job_env`); `release_driver` has its own wire model.
- Test-critic (mental mutation testing, ~85 mutants run) survivors now covered:
  `test_resume_uses_build_cache_mode`, `test_detach_records_cache_mode_and_flags`,
  `test_resume_cached_failure`, `test_resume_skipped_missing_input_fails`,
  `test_resume_refines_through_chain`, `test_cli_drivers_handle_sigterm[*]`,
  `test_adopt_recorded_jobs` (array tasks), `test_resume_refuses_pending_job_it_cannot_attach`,
  `test_lease_renewal_error_tolerated`, and per-build release + expiry boundary in the contract
  `test_driver_lease`. Still uncovered (low risk): the domain check in `resume_build`, signal
  handlers restored when no signal arrived, the pg share lock under concurrent claims.
- Follow-ups: (0) the review's remaining NITs: `ebs logs` in the e2e test, unit tests of the
  `--detach` submission-failure path and `[driver]` validation, re-checking `get_result` before
  counting an adopted job sacct forgot as lost; (1) the real `ebs-runner` still exits 64 on `--array-map` ("arrives with task
  P1-04") although P1-04 is done: every SLURM job array fails once and its actions pass on the
  driver's infra retry as plain jobs. Found by `test_detach_submits_driver_job`, which uses a
  one-row matrix until it is fixed. (2) `ebs status` could show the driver job id and owner
  (from `driver.out` / the lease).
