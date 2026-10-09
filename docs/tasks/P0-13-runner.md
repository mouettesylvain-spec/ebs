# P0-13 — Runner (local lifecycle)

Status: done · Phase: 0 · Depends on: P0-09, P0-10, P0-12 · Parallel-safe with: P0-06, P0-08 · Size: L

## Goal
`ebs-runner` executes one action from a plan in an isolated scratch directory, with only declared
inputs and env, then hashes, uploads and reports results. The same runner is later used inside SLURM jobs.

## Read first
- docs/architecture.md § "SLURM execution, licenses and sandboxing" → "Runner lifecycle on a node" and
  "Sandboxing phases" (v1 strict staging)
- docs/design/interfaces.md §5, §6 (ResultManifest), §7 (classify), §9 (runner CLI + exit codes)
- invariants I10, I11

## Scope (files)
- create `src/ebs/runner/main.py` (argparse, not Typer — minimal deps), `stage.py`, `env.py`, `run.py`,
  `collect.py`, `result.py`
- create `tests/unit/runner/*`, `tests/integration/test_runner_local.py`

## Out of scope
Large-input bind mounts, SLURM traps/epilog, `/debug` live log (P1-04). Bubblewrap (P3-01). The metadata
client is the injected `MetadataStore` (direct PG in P0; HTTP in P1-06).

## Requirements
- R1 Loads the plan from CAS by digest and selects the action by id; unknown id ⇒ exit 64.
- R2 Creates `<scratch>/<build>/<action-hash>/{work,home,tmp,logs}`; `work/` is the tool's cwd; all inputs
  are materialized at their logical paths under `work/`; config files are written; nothing else exists in `work/`.
- R3 I10: every input is verified against its expected digest after materialization (files: content hash;
  trees: manifest digest of what was materialized); mismatch ⇒ exit 76 and no result posted.
- R4 I11: env = toolchain env + declared step env + `HOME=<home>`, `TMPDIR=<tmp>`, `EBS_CPUS`, `EBS_ACTION_ID`,
  `EBS_SCRATCH`; nothing inherited from the caller except `LM_LICENSE_FILE`/`*_LICENSE_FILE` variables and
  `SLURM_*` (listed in config `[runner].passthrough_env`); passthrough vars are not in the key.
- R5 Executes argv with a wall-clock timeout (`resources.time`), combined stdout/stderr to `logs/tool.log`
  with a size cap (`[runner].max_log`, default 2 GiB, tail kept), process-group kill on timeout
  (SIGTERM, 30 s grace, SIGKILL).
- R6 Classifies with the rule's `classify`; for PASSED/FAILED: hashes outputs (missing non-optional output ⇒
  FAILED with summary `missing_output=<name>`), uploads outputs + log to CAS, posts the ResultManifest via
  `record_result` and `cache_put` (if cache mode is `write`), exits 0. For INFRA or timeout: posts an
  `infra_failed` event with reason, uploads the log for debugging, exits 75, writes no cache entry.
- R7 Output ids: deterministic ⇒ content digest; `deterministic: false` ⇒ `nondeterministic_output_id`.
- R8 Scratch removed at the end in all of: pass, fail, infra, timeout, exception (try/finally + signal
  handler for SIGTERM/SIGINT that kills the child group first). `--keep-scratch` keeps it and prints its path.
- R9 Resource usage from `os.wait4` rusage (max RSS, CPU) and wall time recorded in the manifest.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_main.py::test_unknown_action_exit_64` | R1 | unit |
| `test_stage.py::test_only_declared_inputs_present`, `::test_config_files_written` | R2 | unit |
| `test_verify_inputs.py::test_mismatch_exit_76_no_result` | R3 (I10) | unit |
| `test_env_scrub.py::test_env_exact` (tool = `env -0` script), `::test_home_empty`, `::test_passthrough` | R4 (I11) | unit |
| `test_run.py::test_timeout_kills_group` (spawns grandchild), `::test_log_cap_keeps_tail` | R5 | unit |
| `test_run.py::test_pass_fail_infra_paths[...]`, `::test_missing_output_fails` | R6 | unit (InMemoryMetadataStore, tmp CAS) |
| `test_result.py::test_output_ids` | R7 | unit |
| `test_cleanup.py::test_cleanup_all_paths[...]` (incl. SIGTERM to runner) | R8 | integration |
| `test_result.py::test_rusage_recorded` | R9 | unit |

## Done when
- [x] `make check-all` passes (locally without Postgres: PG contract/migration tests skipped, see notes); runner imports only allowed modules (layering contract `runner-imports-only-allowed` added)

## Notes

Implementation notes (P0-13):
- CONTRACT CHANGES (human decisions, 2026-10-05):
  - `PlanToolchain.env` (interfaces.md § 4): the plan carries each toolchain's captured,
    HOME-normalized env; plan.json `toolchains` entries are `{module, id, env}`. Already hashed
    into the toolchain id, so no action key changes (golden keys untouched, no KEY_SCHEMA bump);
    the golden plan snapshot was regenerated for the new field only.
  - `MetadataStore.resolve_output(domain, object_id) -> Digest | None` (§ 6): content digest of
    the `out` edge with that passed-down id; the runner uses it to stage `deterministic: false`
    inputs. Memory + PG + contract tests. The PG side was NOT run locally (no Postgres/Docker):
    run `make check-all` with `EBS_TEST_PG_URL` before merging.
- `ebs.config` was a stub: added `config_paths`/`load_config` (lookup order of overview.md);
  consumers validate their own sections (`RunnerSettings.from_config`). New `[runner]` keys
  `passthrough_env`, `max_log`, `kill_grace_s` (overview.md).
- R8 tests are in `tests/integration/test_runner_local.py` (the task table named
  `test_cleanup.py`); they run `python -m ebs.runner.main` as a subprocess, incl. SIGTERM/SIGINT
  and repeated SIGTERM.
- Choices: PATH defaults to `/usr/bin:/bin` when no layer sets it; config files are written
  read-only; inputs are never symlinked out of the CAS in P0 (tree files may be hard links);
  scratch build dir is `local` without `--build`, and without `--build` the manifest is printed
  on stdout instead of posted. Exec failures (program not found) are INFRA, not FAILED: never
  cache a node-dependent failure (reviewer suggested 64; kept 75 and documented).
- `tests/unit/test_layering.py` failed under `FORCE_COLOR` (ANSI codes broke the `BROKEN`
  match, also on main); it now strips ANSI codes. Same assertions.

Follow-ups / open questions:
- Executable bit of source inputs (P0-08 follow-up) is still open: staged source files are 0444,
  so a Makefile that runs `./script.sh` from the sources fails. Needs `InputRef.executable` + key
  schema bump.
- `--array-map` (P1-04) exits 64 for now; large-input bind mounts, `/debug` collection, SLURM
  epilog cleanup are P1-04.
- An unknown `--build` id surfaces as MetadataError → exit 75 (retried); distinguishing "unknown"
  from "store down" needs a `NotFound` error subclass in `ebs.meta` (P1-06?).
- The runner does not re-derive the toolchain id from `PlanToolchain.env` (version/fingerprint
  are not in the plan); it only checks the action's toolchain id is the plan's.
- The "bytes dropped" marker line is written in addition to `max_log` bytes of tail.
- In-memory `resolve_output`'s `direction == "out"` and `content is not None` checks are
  redundant with each other (in edges never carry content), so neither is mutation-killable.
- The driver (P0-15) must refine and store the plan before submitting actions whose inputs are
  outputs of deterministic producers (the runner refuses `key=None` with exit 64), and must set
  `running`/`done`/`failed` itself (`record_result` never changes state).
