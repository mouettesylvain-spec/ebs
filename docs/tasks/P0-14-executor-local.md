# P0-14 — Executor API and local executor

Status: review · Phase: 0 · Depends on: P0-13 · Parallel-safe with: P0-08 · Size: S

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
- [x] `make check` passes (PG migration/contract cases skipped locally, see Notes)

## Notes

Implementation notes (P0-14):
- CONTRACT CHANGES (human decisions, 2026-10-06):
  - Runner infra line (interfaces.md § 9): with exit 75 the runner's last stderr line is
    `ebs-runner: infra_failed {"detail", "reason"}`; `format_infra_line`/`parse_infra_reason` in
    `ebs.runner.errors`. The runner only emitted the reason to MetadataStore (no read API, and
    nothing without `--build`), so R3's "reason from the runner's event" had no channel. It is
    written before the store event, so a store outage cannot turn it into `other`.
  - `InfraReason` gains `input_verification` (§ 6/§ 8) for runner exit 76, with Alembic revision
    `0002_infra_input_verification` (CHECK rewrite; downgrade maps such rows to `other`). The PG
    side (migration tests, PG contract cases) was NOT run locally (no Postgres/Docker): run
    `make check-all` with `EBS_TEST_PG_URL` before merging.
- `JobHandle`/`SubmitBatch` were named but undefined in § 8; defined there now (frozen
  dataclasses; a batch is non-empty, one step, one domain, unique action ids).
- Choices: FIFO with head-of-line blocking (no starvation of wide actions); cpus above
  `max_parallel` are capped (run alone, warning); cancelled jobs keep their slots until the runner
  exits; `close(grace_s)` uses one deadline for all runners; runner stdout/stderr are kept under
  `<log_dir>/local-*/` (`log_paths`; without `--build` stdout holds the ResultManifest JSON).
- Follow-ups: P0-15 must tolerate a posted result for a job reported `cancelled` (the runner
  posted, then got SIGTERM). P1-05 decides the retry policy for `input_verification` (TODO added
  there). A runner SIGKILLed by `close` while its tool still runs orphans the tool (the tool has
  its own session); rare, because the runner kills the tool group as soon as SIGTERM arrives.
