# P0-15 — Driver / scheduler

Status: todo · Phase: 0 · Depends on: P0-08, P0-10, P0-14 · Parallel-safe with: — · Size: L

## Goal
Walk the plan, satisfy what the cache already knows, submit only ready cache misses, react to results,
refine keys, retry infrastructure failures, and keep an event log — the loop that makes a build.

## Read first
- docs/architecture.md § "SLURM execution…" first paragraph (no SLURM dependencies; driver owns the DAG),
  § "Hashing…" → "Cache semantics"
- docs/design/interfaces.md §4 (refine), §6, §8, §10; invariant I12

## Scope (files)
- create `src/ebs/driver/scheduler.py`, `cache_policy.py`, `retry.py`, `events.py`, `build.py`
- create `tests/unit/driver/*` using `InMemoryMetadataStore`, tmp CAS and a `ScriptedExecutor` test double
  (in `tests/fakes/executor.py`) whose results are declared per action

## Requirements
- R1 An action is submitted only when every input id is known and its key is computed; before submitting,
  `cache_get(domain, key)` is consulted according to the cache mode.
- R2 Cache modes: `off` (never read/write), `read` (read, never write — default for personal builds),
  `write` (read + write — CI/release); `--rerun-failed` bypasses cache hits whose status is `failed`.
- R3 Cache hits mark the action `cached`, feed its output ids to `Planner.refine`, and trigger downstream
  readiness without any executor call.
- R4 Early cutoff: if a deterministic producer reruns and yields the same content digest as before, its
  consumers' keys are unchanged and they hit the cache.
- R5 Failure handling: FAILED actions are final and block their dependents (dependents marked `skipped`);
  `-k/--keep-going` continues independent branches; without `-k` the driver stops submitting new work after
  the first failure, lets running actions finish, then exits 1.
- R6 I12: infra failures are retried up to `max_retries` (default 2) with backoff; retry policy hooks exist
  for P1-05 (OOM ⇒ larger memory); exhausted retries ⇒ action `infra_failed`, build exits 3.
- R7 Submission batching: ready actions of the same step and identical resources/licenses are grouped into one
  `SubmitBatch` (size ≤ `max_batch`), so P1-02 can map batches to job arrays.
- R8 Events (interfaces.md §10) are written to MetadataStore and to `.ebs/builds/<uuid>/events.jsonl`;
  the driver can be interrupted (SIGINT) and cancels submitted work, then records the build as `cancelled`.
- R9 Polling: one `executor.poll` per loop iteration for all in-flight handles; interval adapts between
  `poll_min_s` and `poll_max_s` (FakeClock-driven test).
- R10 Build record: plan digest stored in CAS, `create_build` with git info of the flow repo, `finish_build`
  with final status and counts.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_scheduler.py::test_ready_only_when_inputs_known` | R1 | unit |
| `test_cache_policy.py::test_modes[...]`, `::test_rerun_failed` | R2 | unit |
| `test_scheduler.py::test_cache_hit_skips_submission` | R3 | unit |
| `test_scheduler.py::test_early_cutoff` | R4 | unit |
| `test_scheduler.py::test_failure_blocks_dependents`, `::test_keep_going`, `::test_stop_on_first_failure` | R5 | unit |
| `test_failure_classes.py::test_infra_not_cached_and_retried`, `::test_retries_exhausted_exit_3` | R6 (I12) | unit |
| `test_scheduler.py::test_batching_rules` | R7 | unit |
| `test_events.py::test_jsonl_and_store`, `test_scheduler.py::test_sigint_cancels` | R8 | unit |
| `test_scheduler.py::test_adaptive_polling_single_call` | R9 | unit |
| `test_build.py::test_build_record` | R10 | unit |
| Property: random DAGs (Hypothesis) with random pass/fail/infra outcomes ⇒ every action ends in a terminal
  state, no action runs before its inputs, and cached+run == all non-skipped actions | R1, R5, R6 | property |

## Done when
- [ ] `make check` passes; property test runs 500 examples in < 20 s
