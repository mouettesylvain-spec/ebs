# P1-01 — SLURM CLI adapter and FakeSlurm

Status: review · Phase: 1 · Depends on: P0-01 · Parallel-safe with: everything in P0 · Size: L

## Goal
A typed, tested wrapper over `sbatch`, `squeue --json`, `sacct --json`, `scancel`, `scontrol`, and a
FakeSlurm good enough that every executor behaviour can be tested on a laptop.

## Read first
- docs/architecture.md § "SLURM execution, licenses and sandboxing" → Submission strategy, status tracking
- docs/design/testing.md → FakeSlurm
- SLURM 26.05 man pages for sbatch/squeue/sacct (`--json` output), `--parsable`

## Scope (files)
- create `src/ebs/exec/slurm/cli.py` (adapter), `src/ebs/exec/slurm/models.py` (Pydantic models of the JSON
  fields we use), `src/ebs/exec/slurm/states.py` (state + reason mapping)
- create `tests/fakes/slurm/` (package with console entry points `sbatch`, `squeue`, `sacct`, `scancel`,
  `scontrol`, installed into a temp bin dir by a fixture), `tests/fixtures/slurm/26.05/*.json`
  (captured from a real cluster, anonymized: users `u1…`, nodes `n001…`, no real partition/account names)
- create `tests/unit/exec/slurm/test_cli.py`, `test_states.py`, `tests/integration/slurm/test_fakeslurm.py`

## Requirements
- R1 `submit(script: Path, opts: SbatchOptions) -> JobId` uses `sbatch --parsable` with argv built from typed
  options (partition, account, qos, cpus, mem, time, licenses, constraint, array + throttle, output/error
  paths, job name, export=NONE + explicit env file); never passes user strings through a shell.
- R2 `query(job_ids) -> dict[JobId|ArrayTaskId, SlurmJobInfo]` makes one `squeue --json --jobs=…` call and
  one `sacct --json` call only for jobs no longer in squeue; results parsed with the models; unknown extra
  fields ignored; missing required fields raise `ExecutorError` with the SLURM version.
- R3 State mapping (`states.py`): PENDING reasons `Licenses` ⇒ `licenses`, `Resources`/`ReqNodeNotAvail` ⇒
  `resources`, `Priority` ⇒ `priority`; terminal states OUT_OF_MEMORY ⇒ oom, TIMEOUT ⇒ timeout, NODE_FAIL ⇒
  node_fail, PREEMPTED ⇒ preempted, CANCELLED ⇒ cancelled, FAILED/COMPLETED ⇒ read runner exit code.
- R4 Transient CLI failures (socket timed out, slurmctld not responding) are retried with backoff
  (max 5, FakeClock-tested); permanent errors (invalid account, QOS not allowed) raise immediately with the
  SLURM message.
- R5 FakeSlurm implements the subset in testing.md (arrays with `%N`, licenses pools, mem kill, time kill,
  fault injection via a control file) and its JSON output validates against the same Pydantic models used on
  the captured 26.05 fixtures.
- R6 A `grid`-marked test runs the adapter against the real cluster (submit `sleep 1`, array of 3, cancel)
  in the nightly job.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_cli.py::test_sbatch_argv[...]` (golden argv), `::test_no_shell` | R1 | unit |
| `test_cli.py::test_query_batches_calls`, `::test_parse_fixtures[...]` | R2 | unit |
| `test_states.py::test_mapping[...]` | R3 | unit |
| `test_cli.py::test_transient_retry`, `::test_permanent_error_message` | R4 | unit |
| `test_fakeslurm.py::test_array_throttle`, `::test_license_pool_blocks`, `::test_oom_kill`, `::test_fault_injection[...]` | R5 | integration |
| `tests/grid/test_slurm_real.py` | R6 | grid |

## Done when
- [ ] `make check-all` passes (P1-01 session: `make check` green; integration ran with 30 passed incl. all FakeSlurm tests, but the Postgres tests were skipped: no Docker/PG in that container); fixture capture script `scripts/capture_slurm_fixtures.py` documented

## Notes
- The 26.05 fixtures are hand-written from the documented JSON shape (data_parser v0.0.44): no
  cluster was reachable. `tests/fixtures/slurm/26.05/README.md` says so; replace them with
  `scripts/capture_slurm_fixtures.py` output on the first grid run (P1-12 at the latest) and
  re-check `test_parse_fixtures`. Also confirm there that `--export=NONE` + `--export-file` gives
  the job exactly the file's variables (the grid test asserts it).
- Interpretations: "max 5" = 5 retries (6 attempts), backoff 1/2/4/8/16 s capped at 30 s.
  `query` uses `squeue --states=all` so recently ended jobs need no sacct call. Unknown SLURM
  states map to `infra_failed("other")` with a warning. `--no-requeue` is the default.
- Follow-up for P1-02: a transient `sbatch` failure (socket timeout) is retried, and the
  controller may have accepted the first request, so a job can be submitted twice. P1-02 should
  reconcile by its unique job name (R4 naming) before or after a retried submit, and cancel the
  extra job.
- No new dependencies. D5 (SLURM limits) is not touched here; the adapter takes whatever
  throttle/partition/QOS the executor passes.
