# P1-04 — Runner on the grid: materialization, debug collection, cleanup

Status: done · Phase: 1 · Depends on: P0-13, P1-06 · Size: M

## Goal
The runner behaves well on shared compute nodes: large inputs are never copied needlessly, debug files
come back to NFS, live logs are visible, and node scratch never leaks — even when SLURM kills the job.

## Read first
- docs/architecture.md § "Runner lifecycle on a node" (steps 1–7), § "Artifact storage…" → Read path
- docs/design/invariants.md I16; open decision D2

## Scope (files)
- modify `src/ebs/runner/stage.py`, `collect.py`, `main.py`; create `src/ebs/runner/livelog.py`
- create `deploy/slurm/epilog.d/ebs-scratch-cleanup.sh` + README (admin install)
- tests: `tests/unit/runner/test_materialize_threshold.py`, `test_debug_collect.py`, `test_livelog.py`,
  `tests/integration/test_runner_cleanup.py` (FakeSlurm kills), `tests/unit/deploy/test_epilog.py` (bats-like via subprocess)

## Requirements
- R1 Inputs above `copy_threshold` are symlinked read-only from CAS (`materialize(mode="auto")`); tools that
  need writable copies declare `inputs.<name>.writable: true` (copied). Test with a 300 MB sparse file: no copy.
- R2 `debug.collect` globs (relative to `work/`) are copied to `/debug/<domain>/<build>/<action_id>/` always on
  FAILED/INFRA and on PASSED only if `debug.on_success: true`; total capped at `max_size` (largest files
  truncated to tail with a note); the tool log and a `command.json` (argv, env, input list) are always included.
- R3 D2: live log appended to `/debug/…/live.log`, flushed every 5 s, removed after success unless kept.
- R4 I16: scratch is removed on success, failure, timeout, SIGTERM (SLURM cancel/preempt: handler + 30 s
  grace before SLURM's KillWait) and SIGKILL (epilog removes `$TMPDIR/ebs/<job>` dirs of jobs whose
  comment starts with an EBS build UUID).
- R5 Heartbeat: while running, the runner renews the build lease (P1-09 API; no-op until then) and emits
  `running` with host + SLURM job id once.
- R6 Everything written to `/debug` is created with the domain group and mode 2750/0640.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_materialize_threshold.py::test_large_input_not_copied`, `::test_writable_copy` | R1 | unit |
| `test_debug_collect.py::test_rules[...]`, `::test_size_cap` | R2 | unit |
| `test_livelog.py::test_flush_interval` (FakeClock) | R3 | unit |
| `test_runner_cleanup.py::test_cleanup[success,fail,timeout,sigterm,sigkill+epilog]` | R4 (I16) | integration |
| `test_main.py::test_running_event_once` | R5 | unit |
| `test_debug_collect.py::test_permissions` | R6 | unit |

## Done when
- [x] `make check-all` passes; epilog script reviewed (shellcheck clean)

## Notes (implementation, 2026-10-14)
- R1 needed a flow/plan contract change, agreed with the user: flow `inputs.<name>: {from: …,
  writable: true}` (`InputDef`), `InputRef.writable` (not in the key), plan.json `"writable": true`
  only when set (existing plan digests unchanged). See interfaces.md § 3, § 4.
- R4: the epilog finds a job's scratch through `.ebs-job` (SLURM job id) written by the runner in
  each action dir, deletes as the job user via `setpriv`, and uses a non-empty `SLURM_JOB_COMMENT`
  that is not a UUID only as an early "not EBS" exit. FakeSlurm gained `--comment` and an `epilog`.
- R5: after P1-09 merged, the runner holds its own GC lease on the inputs it staged (acquired
  on the first heartbeat, renewed every `heartbeat_s`): the driver's lease id never reaches the
  node. The runner's `running` event has `{host, job_id}`.

## Follow-ups
- P1-02 (merged) sets `--comment=<build uuid>` and `exec`s the runner: both fit the epilog.
- `ebs-runner --array-map` (interfaces.md § 9 says P1-04) is not in this task's requirements and
  still exits 64, but the merged P1-02 executor submits job arrays with it: needed before arrays
  work end to end on the grid.
- `ebs logs -f` (cli/logs.py) should tail `live.log` from the debug area while an action runs.
- Open design question (key rules): a tool that writes into a read-only staged input fails with
  EACCES and that FAILED result is cached; declaring the input `writable: true` (or changing
  `[cas].copy_threshold`) keeps the same key, so the failure stays cached until `--rerun-failed`.
  Options: classify writes to read-only inputs as infra, or key the staging mode.
