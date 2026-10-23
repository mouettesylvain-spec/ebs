# P3-06 — Pilot worker pool

Status: done · Phase: 3 · Depends on: P1-02 · Size: L

## Goal
Tiny actions (< 30 s) and very large regressions stop paying per-job scheduling overhead: a pool of pilot
jobs pulls actions from a per-build queue.

## Read first
- docs/architecture.md § "SLURM execution…" → Submission strategy (tiny actions row), § "Risks" (submission limits)

## Scope (files)
- create `src/ebs/exec/pilot/{executor,worker,queue}.py` (queue in PostgreSQL with `SELECT … FOR UPDATE SKIP LOCKED`
  via the metadata service); tests `tests/unit/exec/pilot/*`, `tests/integration/slurm/test_pilot.py`

## Requirements
- R1 `PilotExecutor` submits N pilot jobs (array) sized by step resources; each pilot loops: lease an action,
  run `ebs-runner` for it, report, repeat; exits when idle for `idle_s` or walltime nearly exhausted.
- R2 Actions are leased with a visibility timeout; a pilot dying mid-action makes the action re-queue (infra retry).
- R3 Pilots only take actions whose licenses/resources match their allocation; licenses stay requested per pilot
  job via `-L` (documented trade-off: licenses held while idle ⇒ short idle timeout).
- R4 Driver chooses pilot vs direct submission per step (`execution: auto|direct|pilot`; `auto` uses pilot when
  expected action runtime from history < 30 s or count > `pilot_threshold`).
- R5 Passes the executor contract suite.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `tests/integration/slurm/test_pilot.py::test_pool_drains_queue` | R1 | integration |
| `::test_pilot_death_requeues` | R2 | integration |
| `tests/unit/exec/pilot/test_matching.py` | R3 | unit |
| `tests/unit/driver/test_execution_choice.py` | R4 | unit |
| `tests/contract/test_executor.py[pilot]` | R5 | contract |

## Notes

- Decisions (2026-10-20):
  - The queue is a metadata-store contract (`PilotQueueStore`, part of `MetadataBackend`):
    memory, PostgreSQL (`pilot_queue`, migration 0014, leasing with `FOR UPDATE SKIP LOCKED`) and
    HTTP (`/domains/{d}/pilot-queues/{pool}/…`, new authz operation `queue_write` = domain
    membership). A pool is a UUID per executor, so `build=None` runs work too.
  - R2: an expired lease makes the item `lost`; the executor reports `infra_failed("node_fail")`
    and the driver's P1-05 retry re-queues it. Re-queuing silently inside the queue would need a
    `running -> pending` action transition, which the store's state machine forbids.
  - R4: `execution` is a field of `resources` (like `retry_on_timeout`): merged with toolchain
    defaults, written to plan.json only when set, never in the action key (I3, no
    `KEY_SCHEMA_VERSION` bump; `test_key_sensitivity` covers it). "Expected runtime" is the upper
    median wall time of the step's last runs in the work dir's usage log (at least 5 runs).
    The count rule compares the batch size (the scheduler caps batches at 1000), so the default
    `[pilot].threshold` is 200.
  - New dependency: none. New console script `ebs-pilot`.
- Review (task-reviewer) handled: BLOCKER, `poll` topped up pilots from stale item states and
  started an extra pilot after a pilot death (`test_pilot_death_requeues` caught it): items are
  refreshed first now. SHOULD-FIX: retries now carry the excluded nodes into the queue
  (`QueueItem.exclude`, matched by `slot_fits` and the SQL;
  `test_excluded_nodes_follow_the_action`, contract `test_pilot_queue_matching`); threshold
  default lowered below the batch cap; a pilot that cannot spawn runners stops instead of
  failing the whole queue. Also found by the new unit tests: a failed renewal (store
  unreachable) counted as renewed, so a cut-off pilot never stopped its runner.
- Test-critic (mental mutation testing; 11 surviving mutants) now covered: toolchain-default
  `execution` through the planner (`test_planner`; it also found that the merge refused any
  toolchain string, so a toolchain `execution` failed planning: fixed), contract
  `test_pilot_queue_matching_edges` (missing license, exact memory) and
  `test_pilot_queue_expiry_boundary`, consecutive pilot failures, cancel with busy pilots,
  cancel after finish, a pilot SLURM forgets, the default threshold, no walltime left, idle
  time counted from the last action, and the `ebs-pilot` arguments parsed back
  (`parse_pilot_arguments`).
- Follow-ups:
  1. A resumed driver (P2-08) reruns pilot actions but leaves the old pool's pilots draining
     the old queue (duplicate runs, licenses held up to `idle_s`). Record the pool and pilot job
     ids in the build so `resume` can `queue_cancel` and scancel them.
  2. `pilot_queue` rows are never deleted; prune final rows (e.g. in `ebs gc`).
  3. A pilot SLURM reports failed still holds its leases until their TTL (60 s) before the
     actions become `lost`; a `queue_expire(pilot)` call could make that immediate.
  4. A pilot measures its walltime from process start, not job start (`SLURM_JOB_START_TIME`).
  5. The `count > threshold` rule sees one batch, not the step's total actions.
