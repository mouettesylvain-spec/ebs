# P0-13 — Runner (local lifecycle)

Status: todo · Phase: 0 · Depends on: P0-09, P0-10, P0-12 · Parallel-safe with: P0-06, P0-08 · Size: L

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
- [ ] `make check-all` passes; runner imports only allowed modules (layering contract added)
