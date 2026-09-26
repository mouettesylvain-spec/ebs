# P1-02 — SLURM executor (sbatch, job arrays, polling, cancel)

Status: todo · Phase: 1 · Depends on: P1-01, P0-14 · Size: L

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
| `test_executor.py::test_single_query_per_poll` | R5 | integration (FakeSlurm) |
| `test_executor.py::test_cancel_all` | R6 | integration |
| `test_executor.py::test_job_ids_recorded_before_return` | R7 | unit |
| `test_executor.py::test_rate_limit_and_backpressure` (FakeClock) | R8 | unit |
| `tests/contract/test_executor.py[slurm]` | all | contract |

## Done when
- [ ] `make check-all` passes; a 2,000-action fake regression completes through FakeSlurm in < 60 s
