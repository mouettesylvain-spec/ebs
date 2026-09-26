# P3-06 — Pilot worker pool

Status: todo · Phase: 3 · Depends on: P1-02 · Size: L

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
