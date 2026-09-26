# P1-10 — `ebs debug` and `ebs reproduce`

Status: todo · Phase: 1 · Depends on: P1-04, P0-16 · Size: M

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
- [ ] `make check-all` passes
