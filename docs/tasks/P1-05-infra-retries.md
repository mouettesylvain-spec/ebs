# P1-05 — Infrastructure failure classification and retries

Status: review · Phase: 1 · Depends on: P1-02, P0-15 · Size: S

## Goal
Infrastructure problems (OOM, node failure, preemption, license checkout errors, tool crashes) are retried
automatically and sensibly, never cached, and clearly reported.

## Read first
- docs/architecture.md § "Cache semantics" (infrastructure failure bullet); invariant I12

## Scope (files)
- modify `src/ebs/driver/retry.py`; create `src/ebs/driver/resources_tuning.py`
- tests in `tests/unit/driver/test_retry.py`, `tests/integration/slurm/test_retry_e2e.py`

## Requirements
- R1 Retry policy per infra reason (config `[retry]`): `oom` ⇒ memory × 1.5 (cap `max_mem`), `timeout` ⇒ not
  retried by default (usually a hang) but still never cached — final state `infra_failed(timeout)`; a step may
  set `retry_on_timeout: true` — `node_fail`/`preempted`/`runner_crash` ⇒ same resources, `license` ⇒ backoff
  60 s × attempt. TODO (from P0-14): decide `input_verification` (runner exit 76, staged bytes do not match
  their digest: CAS corruption or a stale NFS view) — e.g. retry once on another node, then fail loudly.
- R2 Retried attempts are separate rows/events with attempt number; final status shows the reason history.
- R3 Nodes that failed an action twice with `node_fail` are excluded (`--exclude`) for that build.
- R4 Resource usage from successful manifests is stored; `ebs plan` prints a hint when requested memory is
  > 4× the p95 observed for that step (tuning, not enforcement).
- R5 A test FAILED result is never retried (I12).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_retry.py::test_policy[...]` | R1 | unit |
| `test_retry.py::test_attempt_history` | R2 | unit |
| `test_retry.py::test_node_exclusion` | R3 | unit |
| `tests/unit/driver/test_resources_tuning.py::test_hint` | R4 | unit |
| `test_retry.py::test_failed_not_retried`, `test_retry_e2e.py::test_oom_then_success` (FakeSlurm) | R5, R1 | unit/integration |

## Done when
- [x] `make check-all` passes (except `test_slurm_executor.py::test_fake_regression_2000`, which
  hits its 60 s budget on this machine on `main` too; see Notes)

## Notes
- P1-02 was still `review` in the index but merged to main (PR #16); the task proceeded on it.
- R1 decisions:
  - `input_verification` (the P0-14 TODO): retried `input_verification_retries` times (default
    once), each attempt excluding the node of the failed one; then final, with a `hint` in the
    `finished` event (CAS corruption or a stale NFS view). The bound counts only earlier
    input_verification failures (`RetryPolicy.decide(..., earlier=)`), not other reasons.
  - An OOM of a step without `resources.mem`, or already at `[retry].max_mem`, is final: a retry
    would OOM again. `max_mem` has no default (no cap); `max_retries` bounds the growth.
  - `retry_on_timeout` is a `resources` field (scheduling, not in the key; a toolchain default
    the step overrides) rather than a top-level step field, so it reuses the resource plumbing.
  - Every reason is bounded by `[retry].max_retries`; license waits are not capped by
    `backoff_max_s`.
  - `run_build` keeps `BackoffRetry` (P0) as its default; `ebs build` uses `ReasonRetry`.
- R2: the "separate rows" are the events (one `infra_failed` per attempt, numbered); the
  `actions` table keeps one row per action with its `attempts` count (no schema change).
- R3: "failed an action twice" is read as two `node_fail`s on one node in one build, of any
  actions (a broken node failing two different actions is excluded too).
- R4: usage is stored per work directory in `.ebs/usage.jsonl`, not in the metadata store
  (no store contract change). Hints need at least 5 samples.
- Out-of-scope fixes in tests: an autouse fixture in `tests/conftest.py` restores the structlog
  config after each test (CLI tests leaked a level filter into later `capture_logs` tests, an
  order-dependent failure that also exists on main); the CLI test site sets
  `[stat_cache].audit_fraction = 0` (the 1 % random audit made
  `test_plan.py::test_rehash_ignores_poisoned_stat_cache` fail about 1 % of runs).

### Follow-ups
- Store resource usage in the metadata store (a per-step query), so hints are shared across
  users and CI instead of per work directory.
- Time and CPU tuning hints (only memory is hinted now).
- `test_fake_regression_2000` runs at ~57–62 s against its 60 s budget on a WSL2 workstation,
  on main as well: profile FakeSlurm/the executor, or size the budget per machine.
- Whoever wires the SLURM executor into `ebs build` should keep passing `[retry]`'s policy (done
  here for the local executor path, which is the only one wired today).
