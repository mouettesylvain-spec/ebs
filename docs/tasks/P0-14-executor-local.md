# P0-14 — Executor API and local executor

Status: todo · Phase: 0 · Depends on: P0-13 · Parallel-safe with: P0-08 · Size: S

## Goal
The executor abstraction the driver talks to, and a local implementation that runs `ebs-runner`
subprocesses with bounded parallelism — used for phase 0, for tests and for `ebs reproduce`.

## Read first
- docs/design/interfaces.md §8, §9

## Scope (files)
- create `src/ebs/exec/api.py`, `src/ebs/exec/local.py`
- create `tests/unit/exec/test_local.py`, `tests/contract/test_executor.py` (parametrized; FakeSlurm-backed
  executor joins in P1-02)

## Requirements
- R1 `submit` starts at most `max_parallel` runner processes (default: CPU count), queues the rest,
  honours per-action `resources.cpus` as slots.
- R2 `poll` returns states for all handles in one call without blocking; `pending_reason="resources"` for
  queued actions.
- R3 Maps runner exit codes: 0 ⇒ done, 75 ⇒ infra_failed(reason from the runner's event, default "other"),
  76 ⇒ infra_failed("input_verification"), anything else/signal ⇒ infra_failed("runner_crash").
- R4 `cancel` terminates the runner (which cleans scratch) and reports `cancelled`.
- R5 Local memory limits are advisory (logged), not enforced; the executor documents this.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_local.py::test_parallelism_bound` (fake runner command that records concurrency) | R1 | unit |
| `test_local.py::test_poll_nonblocking_and_pending_reason` | R2 | unit |
| `test_local.py::test_exit_code_mapping[...]` | R3 | unit |
| `test_local.py::test_cancel` | R4 | unit |
| `tests/contract/test_executor.py` (submit/poll/cancel semantics with a scripted fake runner) | R1–R4 | contract |

## Done when
- [ ] `make check` passes
