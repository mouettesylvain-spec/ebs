# P1-10 — `ebs debug` and `ebs reproduce`

Status: done · Phase: 1 · Depends on: P1-04, P0-16 · Size: M

## Goal
Any failing action can be inspected and re-run bit-for-bit from a login node, including interactively
(e.g. Questa GUI on the failing test), without digging through SLURM spool dirs.

## Read first
- docs/architecture.md § "Debugging, CLI, dashboard and CI" → Core CLI (`debug`, `reproduce`)

## Scope (files)
- create `src/ebs/cli/debug.py`, `src/ebs/cli/reproduce.py`, `src/ebs/runner/reproduce.py`
- tests `tests/unit/cli/test_debug.py`, `test_reproduce.py`, `tests/integration/test_reproduce.py`

## Requirements
- R1 `ebs debug ACTION [--build B]` prints: status + reason, first error signature, argv (shell-quoted),
  declared env, toolchain module/id, input list with digests and origins (source file / producer action /
  import), SLURM job id and node, debug dir path; `--json` too.
- R2 `ebs reproduce ACTION [--dir D] [--interactive] [--salloc]` rebuilds the exact sandbox (same staging and
  env as the runner, via shared code) in local scratch or in `D`, prints what it did, and either runs the
  command (`--run`) or drops into `bash --norc` with the sandbox env (`--interactive`); `--salloc` wraps it
  in `salloc` with the action's resources, licenses and account.
- R3 A reproduced run of a cached/passed action produces identical deterministic outputs (compare digests).
- R4 Reproduce never writes to the action cache (cache mode forced to `off`) and marks its build `kind=repro`.
- R5 Works for actions whose inputs were GC'd only if still available; otherwise explains which inputs are
  missing and suggests `ebs build --no-cache ACTION`.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_debug.py::test_output_fields`, `::test_json` | R1 | unit |
| `test_reproduce.py::test_sandbox_equivalent_to_runner` (compare staged tree + env), `::test_salloc_argv` | R2 | unit |
| `tests/integration/test_reproduce.py::test_identical_outputs` | R3 | integration |
| `test_reproduce.py::test_no_cache_writes` | R4 | unit |
| `test_reproduce.py::test_missing_inputs_message` | R5 | unit |

## Done when
- [x] `make check-all` passes (P1-10 session: unit + perf + integration with Postgres; only the
  nightly real-Verilator e2e tests skipped)

## Notes
- **R4 decision (user, 2026-10-17):** builds have no `kind` field, so reproduce creates no build
  and writes nothing to the metadata store (no build, result, cache entry, event, lease or
  touch). The sandbox's `repro.json` records `kind: "repro"`, `cache_mode: "off"`, the build,
  action, plan and key. No `BuildCreate` contract change. If a stored `kind=repro` build is
  wanted later (e.g. for the dashboard), it needs a migration and a CONTRACT CHANGE.
- **`--salloc` decision (user):** `salloc <job opts> srun --pty ebs reproduce … --build <uuid>
  --run|--interactive`, so the sandbox is built on the node. The default sandbox carries the
  `.ebs-job` marker under SLURM, so the epilog cleans it when the allocation ends (I16).
- Shared runner code for R2: `resolve_content`, `prepare_work`, `remove_tree`
  (`ebs.runner.stage`) and `toolchain_env` (`ebs.runner.env`); `Runner._run` uses them too.
- Default sandboxes go to a new `<[scratch].dir>/repro-<uid>/<action dir>-<random>` (0700) each
  time, so a kept sandbox is never replaced and other users' are never touched.
- Follow-ups:
  - `salloc_argv` renders options itself, mirroring `ebs.exec.slurm.cli.sbatch_argv`; a shared
    option renderer (built from `SbatchOptions`, with its validators) would keep them in step. **Done (2026-10-18):** `job_options`.
  - `--salloc` uses the action's own time limit (+ grace); an interactive GUI session may need a
    `--time` override. **Done (2026-10-18):** `--time`.
  - `ebs reproduce --run` exits 0 whatever the comparison; a non-zero exit when outputs differ
    would let CI use R3 directly. **Done (2026-10-18):** exit 1.
  - Reproduce holds no GC lease on the inputs it links from the CAS (leases need a running
    build); a sweep during a long interactive session could remove linked blobs.
    Still open (2026-10-18): a touch or lease would write to the store, which the R4 decision
    rules out; it needs the human's call (copy large inputs instead, or allow a touch).
  - A build that did not finish has no `final_plan`; reproduce then fills unset input ids from
    the action's recorded result only, so an infra-failed action of an interrupted build may not
    be reproducible.
  - `ebs build` still only offers the local executor (`cli/_context.py` `EXECUTORS`); P1-12
    needs the SLURM executor wired into the CLI. **Done by P2-08** (`--executor slurm`).
