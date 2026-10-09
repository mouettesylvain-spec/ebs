# P0-16 — CLI: `plan`, `build`, `status`, `logs`

Status: done · Phase: 0 · Depends on: P0-15 · Parallel-safe with: — · Size: M

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
- R7 (human decision 2026-10-05) The stat cache defaults to local disk, never to `~/.cache` (often
  NFS, where SQLite WAL is unsafe): `[stat_cache] path` from the config if set, else
  `/var/tmp/ebs-<uid>/statcache.sqlite`, with the directory created mode 0700. A missing or unwritable
  default degrades to rehashing with a warning, never to a failed plan.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_plan.py::test_summary`, `::test_diff_reasons`, `::test_json_schema_stable` | R1 | unit |
| `test_build.py::test_exit_codes[...]`, `::test_non_tty_events` | R2 | unit |
| `test_status.py::test_pending_reasons_split` | R3 | unit |
| `test_logs.py::test_prefix_match`, `::test_follow` | R4 | unit |
| `test_errors.py::test_flow_error_rendering` | R5 | unit |
| `test_statcache_path.py::test_default_is_local`, `::test_config_override`, `::test_unwritable_degrades` | R7 | unit |
| `tests/e2e/test_cli_local.py::test_plan_build_status_logs` | R1–R4 | e2e |

## Done when
- [x] `make check-all` passes; `ebs --help` output reviewed for clarity (paste it in the MR)

## Notes
- CONTRACT CHANGE (R7, human decision 2026-10-05): `default_statcache_path(env)` →
  `default_statcache_path(uid)` = `/var/tmp/ebs-<uid>/statcache.sqlite`; the old `~/.cache` test
  was replaced. The CLI uses the default only in a 0700 directory owned by the user (not a
  symlink), else warns and falls back to an in-memory stat cache. interfaces.md § 12.
- New CLI contract, interfaces.md § 14: `ebs plan --json` document, `.ebs/builds/<uuid>/build.json`
  (CLI-owned, next to the driver's `events.jsonl`; `status`/`logs` work without the store),
  error JSON, `build --json` lines. Status `interrupted` = a `running` build whose driver PID on
  this host is gone.
- No driver change: `ebs build` sees events through a MetadataStore proxy (`_Tap`) wrapping
  `emit`, which is how the UUID is printed before anything runs.
- Runner change (R4): `_CappedLog` opens `tool.log.cur` unbuffered (and loops on partial raw
  writes) so `ebs logs -f` sees output as the tool writes it; before, it lagged by up to 8 KiB.
- Wiring module `cli/_context.py` (`SiteServices`) and `cli/_records.py` were added beyond the
  listed files; `[rules]` config is now wired (overview.md said "config wiring: P0-16").
- Typer 0.27 vendors click (`typer._click`): never import `click` in `ebs.cli` (its Context and
  exceptions are other classes; `click_type=Choice` errors escape as exit 1). Choices are Enums.
- Follow-ups:
  - `ebs plan` predicts hits with `cache_get`, which counts a hit and touches `last_access`;
    a side-effect-free `cache_peek` on MetadataStore would keep hit statistics honest.
  - `logs -f` is local-executor only; grid live logs come with D2/P1-04. At the `max_log` cap a
    rotation in the last poll interval hides that interval's tail (see `ebs.cli.logs`).
  - `tests/unit/plan/test_planner.py::test_refine_idempotent` (perf, 200 ms Hypothesis deadline)
    failed 2 of 3 runs on this machine under load; untouched by this task.
