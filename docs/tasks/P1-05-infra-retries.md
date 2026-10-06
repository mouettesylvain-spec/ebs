# P1-05 — Infrastructure failure classification and retries

Status: todo · Phase: 1 · Depends on: P1-02, P0-15 · Size: S

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
- [ ] `make check-all` passes
