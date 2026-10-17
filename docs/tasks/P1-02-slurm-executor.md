# P1-02 — SLURM executor (sbatch, job arrays, polling, cancel)

Status: done · Phase: 1 · Depends on: P1-01, P0-14 · Size: L

## Goal
Run actions on the grid: one job per heavy action, one job array per batch of similar actions, a single
status query per poll, clean cancellation.

## Read first
- docs/architecture.md § "SLURM execution…" → Submission strategy table and status paragraph
- docs/design/interfaces.md §8, §9 (array mode of the runner)

## Scope (files)
- create `src/ebs/exec/slurm/executor.py`, `arrays.py`, `jobscript.py`
- add `SlurmExecutor` to `tests/contract/test_executor.py` (backed by FakeSlurm)
- create `tests/unit/exec/slurm/test_executor.py`, `test_arrays.py`, `test_jobscript.py`

## Requirements
- R1 Batches with 1 action ⇒ single `sbatch`; batches with > 1 action ⇒ one array job; the array map (index ⇒
  action id) is stored in CAS and passed as `--array-map`; arrays larger than `max_array_size` are split.
- R2 Array throttle `%N` from config (`array_throttle`), overridable per step (`resources.max_parallel`).
- R3 Job script is generated from a fixed template: `set -euo pipefail`, `exec ebs-runner …` with
  argv-quoted arguments (shlex.quote only on values we generate), `--export=NONE`, stdout/stderr to
  `/debug/<domain>/<build>/slurm/%A_%a.out` (paths from config); no user text in the script except quoted values.
- R4 Job names are `ebs:<build-short>:<step>`; comment field carries the build UUID (for admin tooling and
  the epilog in P1-04).
- R5 `poll` issues at most one `query` for all in-flight jobs; array tasks are reported individually.
- R6 `cancel` uses one `scancel` for all job ids; cancelled pending array tasks are reported as `cancelled`.
- R7 Driver restart safety: submitted job ids are recorded in MetadataStore (`actions.slurm_job_id`)
  before `submit` returns, so a restarted driver (P2-08) can re-attach.
- R8 Submission rate limiting: at most `max_submits_per_min` sbatch calls; beyond `max_pending_jobs` in-flight
  jobs, the executor stops accepting batches (driver keeps them queued) — defaults conservative (D5).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_arrays.py::test_single_vs_array`, `::test_split_large_arrays`, `::test_array_map_in_cas` | R1 | unit |
| `test_arrays.py::test_throttle_override` | R2 | unit |
| `test_jobscript.py::test_template_golden`, `::test_quoting_hostile_values` (Hypothesis) | R3 | unit/property |
| `test_executor.py::test_job_name_and_comment` | R4 | unit |
| `tests/integration/slurm/test_slurm_executor.py::test_single_query_per_poll` (+ unit `test_executor.py::test_poll_queries_once_and_maps_states`) | R5 | integration (FakeSlurm) |
| `tests/integration/slurm/test_slurm_executor.py::test_cancel_all` (+ unit `test_executor.py::test_cancel_one_call`, `::test_cancel_when_query_fails`) | R6 | integration |
| `test_executor.py::test_job_ids_recorded_before_return` | R7 | unit |
| `test_executor.py::test_rate_limit_and_backpressure` (FakeClock), `::test_lost_job_leaves_in_flight`; driver: `tests/unit/driver/test_scheduler.py::test_busy_executor_*`, `::test_retry_due_while_executor_busy`, `::test_deferral_not_held_back_by_later_retry`, `::test_refused_retry_abandoned_on_stop` | R8 | unit |
| `tests/contract/test_executor.py[slurm]` | all | contract |

## Done when
- [x] `make check` passes; SLURM integration + contract pass; a 2,000-action fake regression completes through FakeSlurm in < 60 s (~42 s on 4 cores). Postgres integration tests not run here.

## Notes
- D5 defaults used (`[slurm]` keys with `max_array_size = 1000`, `array_throttle = 200`,
  `max_pending_jobs = 2000`, `max_submits_per_min = 60`); `SlurmSettings.from_config` reads them
  plus `[debug].root` and an optional `[slurm].bin_dir`.
- CONTRACT CHANGES (interfaces.md §3, §4, §8, §9): `ExecutorBusy` (R8; the driver keeps a refused
  batch queued, waits `max(retry_after_s, poll_min_s)` and does not re-read the cache for it);
  `Resources.max_parallel` (R2; plan.json writes it only when set, not in the key);
  `SbatchOptions.comment` (R4); the array map format.
- Logs: arrays use `%A_%a.out` as R3 says; plain jobs use `%j.out` (SLURM expands `%a` to
  4294967294 for a non-array job). stdout and stderr share the file so the runner's infra line is
  found there.
- SLURM's time limit is `resources.time + 120 s` so the runner's own timeout fires first and it
  can still upload the log and report.
- `SlurmCli.query` matched each reference against every record (O(refs × records), ~1 s for a
  2,000-task poll); it now indexes the records (same "later record wins" semantics).
- FakeSlurm was too slow for the 2,000-action Done-when check on a 4-core box (~25 jobs/s):
  supervisors are now forked after the scheduling transaction (no interpreter start per job), the
  package `__init__` no longer imports pydantic, cancellation is a marker file instead of a lock
  round trip per poll, and the schedule pass reads less. ~45 jobs/s; the check takes ~42 s.
- The array throttle applies per array part: a batch split at `max_array_size` into k parts can
  run k·N tasks at once (SLURM cannot throttle across arrays).
- R7 writes one `set_action_state` per job (array task): 1,000 store transactions per full
  array. Correct, but a batched state update in the store API would cut it (follow-up).
- A job SLURM no longer knows for `LOST_AFTER_POLLS` (10) polls is reported `infra_failed`
  ("other") and leaves the in-flight count, so back-pressure cannot wedge the driver.
- Follow-ups: wire `--executor slurm` into the CLI (`_context.open_executor`) — not in this task's
  scope; P1-04 must implement `--array-map` in the runner using the map format above (it cannot
  import `ebs.exec`, so it needs its own small decoder); P1-01's note about reconciling a
  duplicated sbatch after a retried socket timeout is not done: job names (R4) are not unique per
  batch, so it needs a unique token (e.g. in the job name or an extra comment field), best done
  with P1-05 retries; P2-08 can re-attach with `actions.slurm_job_id` + `SlurmExecutor.job_ref`.
