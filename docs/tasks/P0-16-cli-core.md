# P0-16 — CLI: `plan`, `build`, `status`, `logs`

Status: todo · Phase: 0 · Depends on: P0-15 · Parallel-safe with: — · Size: M

## Goal
The primary user interface for phase 0: explain a plan, run a build, watch it, read logs.

## Read first
- docs/architecture.md § "Debugging, CLI, dashboard and CI" → Core CLI table
- docs/design/interfaces.md §11 (exit codes)

## Scope (files)
- create `src/ebs/cli/main.py` (extend), `cli/plan.py`, `cli/build.py`, `cli/status.py`, `cli/logs.py`,
  `cli/rules.py` (`ebs rules list`), `cli/_output.py` (rich tables, `--json` output mode)
- create `tests/unit/cli/*` (Typer `CliRunner`) and `tests/e2e/test_cli_local.py`

## Requirements
- R1 `ebs plan [-f flow.yaml] [STEP…] [--rehash] [--json]` prints action counts per step, predicted cache
  hits/misses/unknown, and for each miss the changed key fields vs the last build of the same flow on this
  machine (or vs `--diff BUILD`). `--json` emits a stable machine-readable document (schema in docs).
- R2 `ebs build [STEP…] [-k] [--cache off|read|write] [--no-cache] [--rerun-failed] [--executor local]`
  runs the driver with a live progress view (TTY) or line events (non-TTY), prints the build UUID first,
  and exits with interfaces.md §11 codes.
- R3 `ebs status [BUILD]` (default: last build in this directory) shows per-step counts by state including
  `pending: licenses` vs `pending: resources`; `--watch` refreshes.
- R4 `ebs logs ACTION [-f] [--build BUILD]` prints the tool log from CAS (finished) or follows the live log
  (running, local executor: scratch log file); ACTION accepts a unique prefix of the action id.
- R5 Errors are rendered as one-line messages with location and hint; `--debug` shows tracebacks.
- R6 Every command supports `--json` for scripting, and `NO_COLOR` is honoured.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_plan.py::test_summary`, `::test_diff_reasons`, `::test_json_schema_stable` | R1 | unit |
| `test_build.py::test_exit_codes[...]`, `::test_non_tty_events` | R2 | unit |
| `test_status.py::test_pending_reasons_split` | R3 | unit |
| `test_logs.py::test_prefix_match`, `::test_follow` | R4 | unit |
| `test_errors.py::test_flow_error_rendering` | R5 | unit |
| `tests/e2e/test_cli_local.py::test_plan_build_status_logs` | R1–R4 | e2e |

## Done when
- [ ] `make check-all` passes; `ebs --help` output reviewed for clarity (paste it in the MR)
