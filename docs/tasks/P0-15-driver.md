# P0-15 — Driver / scheduler

Status: done · Phase: 0 · Depends on: P0-08, P0-10, P0-14 · Parallel-safe with: — · Size: L

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
  (in `tests/helpers/driver.py`, the repo's convention; `tests/fakes/` does not exist) whose results are
  declared per action
- added during the task (human-approved contract change): `MetadataStore.get_result`, action state
  `skipped`, migration `src/ebs/meta/migrations/versions/0003_skipped_state_action_result.py`

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
- [x] `make check` passes (apart from pre-existing flaky timing tests, see Notes); property test runs
  500 examples in ~14 s

## Notes
- CONTRACT CHANGE 1 (approved by the human): the driver must read a finished action's manifest in
  every cache mode, but the store only kept it in the action cache (`write` mode). `record_result`
  now also stores it (`actions.result`, migration 0003) and `get_result(build, action_id)` reads it.
- CONTRACT CHANGE 2: R5 says dependents are marked `skipped`, which was not an `ActionState`;
  added (final, only from `queued`), same migration. Downgrade maps it to `cancelled`.
- Decisions: cache lookup only once all producers succeeded (conservative; a nondeterministic
  producer's consumer could look up earlier); cache writes stay with the runner (§ 9), the driver
  calls `record_result` on a hit so `get_result`/provenance work for cached actions; a waiting
  retry stays `infra_failed` until resubmitted and is abandoned if the build stops; exit 3 only
  when retries were really used up; `cancelled` exits 130 (`ExitCode.CANCELLED`, decided by the
  human); a job reported
  `cancelled` without our asking, missing from 10 polls, or `done` without a posted manifest is an
  infrastructure failure; a consumer of a missing optional output is `skipped` and fails the build.
- Review: task-reviewer (no blocker; fixed: no submission after a failure found mid-pass, Ctrl-C
  ends long backoffs at once via an Event on the real clock, exit 3 only for exhausted retries,
  refine only for actions with consumers, warm-cache property runs, internal errors raise
  RuntimeError (exit 4), leaked handles cancelled, lost jobs, KeyboardInterrupt => cancelled,
  second Ctrl-C restores the old handler). test-critic: 15 surviving mutants, all killed and
  re-verified (e.g. status read from exit code, lone cached failure reported as passed, cancelled
  exiting 0, pending -> done crashing the store transition, license counts not splitting batches).
  The warm-cache property test then found a hang (a cached failure late in a pass left an earlier
  waiting action unfinished); fixed with `test_stop_found_late_in_pass_skips_waiting_actions`.
- Follow-ups:
  - Done (human request): `--rerun-failed` in `write` mode replaces the cached failure through the
    new `MetadataStore.cache_replace_failed` (CONTRACT CHANGE 3), so a flaky test that now passes
    is reused by later builds.
  - Pre-existing flaky timing tests under coverage (also on main): P0-08's
    `test_large_plan_perf` (2 s budget) and `test_refine_idempotent` (Hypothesis 200 ms
    deadline). Human to decide how to fix (separate perf run without coverage recommended).
  - Pre-existing P0-09 property failure found by a random example:
    `tests/unit/cas/test_materialize.py::test_resolve_matches_kernel` with links
    `{'l0': 'l1/..', 'l1': 'd/../l0/..'}`: `_resolve` and the kernel disagree on a link cycle.
  - P0-16: map `BuildOutcome.exit_code`, read `.ebs/builds/<uuid>/events.jsonl` with `read_events`.
