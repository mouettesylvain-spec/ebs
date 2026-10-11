# P1-13 — `ebs outputs` and `ebs fetch`

Status: done · Phase: 1 · Depends on: P0-16 · Parallel-safe with: every P1 task except P1-10 (both add a CLI command; trivial merge in `cli/main.py`) · Size: S

## Goal
A user can list what a build produced and copy any output out of the CAS into a directory they
choose, without knowing the CAS layout or querying Postgres. Today a finished build's reports,
models and logs are reachable only by digest (`<cas>/<domain>/blobs/sha256/ab/cd/<hex>`), which
the quickstart cannot explain and users stumble on first.

## Read first
- docs/design/interfaces.md § 5 (CAS: `materialize`), § 6 (`get_result`, `ResultManifest`,
  `OutputResult`), § 14 (CLI: build record, BUILD/ACTION arguments, `--json`, exit codes)
- Existing code to follow: `src/ebs/cli/logs.py` (`find_build`, `match_action`, reading the result
  of one action), `src/ebs/cas/materialize.py`, `src/ebs/cli/_context.py` (`SiteServices.cas`,
  `.store`)

## Scope (files)
- create `src/ebs/cli/outputs.py` (both commands; small shared helpers)
- modify `src/ebs/cli/main.py` (register `outputs`, `fetch`)
- create `tests/unit/cli/test_outputs.py`, `tests/unit/cli/test_fetch.py`
- modify `docs/design/interfaces.md` § 14 (document both commands and their `--json`)
- modify `docs/user/quickstart.md` § 2 (one `ebs fetch` example after the first build; it is run
  by `tests/e2e/test_docs.py`, so keep the console block exact)

## Out of scope
- Fetching another team's release outputs (`ebs release …`, P2-02; imports P2-03).
- Collected debug files under `/debug/…` (`ebs debug`, P1-10).
- Rerunning an action to regenerate outputs (`ebs reproduce`, P1-10).
- Cross-domain copies (`ebs release export`, P2-01/P2-02).
- Recording a GC `last_access` touch for fetched objects: add it in P1-09 if GC needs it
  (note it there; do not add a store method here).

## Contract
CLI only, no new Python API. Add to interfaces.md § 14:

```
ebs outputs [ACTION…] [--build B] [--step S] [--json]
ebs fetch   ACTION[.OUTPUT]… [--build B] [-o DIR] [--flat] [--mode copy|hardlink|symlink] [--force] [--json]
```

`ebs outputs --json` (v1):

```json
{"v": 1, "uuid": "…", "build": 7, "domain": "…",
 "outputs": [{"action_id": "…", "step": "…", "state": "done|cached|failed",
              "name": "report", "path": "lint/reports/alu.rpt", "type": "file|tree",
              "size": 123, "digest": "sha256:…", "id": "sha256:…"}]}
```

`ebs fetch --json`: `{"v": 1, "uuid", "build", "fetched": [{"action_id", "name", "digest", "dest"}]}`.

## Requirements
- R1 `ebs outputs` lists, for every action of the build (default: the latest, as § 14) that has a
  recorded result (`get_result`; `done`, `cached` and test-`failed` actions alike), one row per
  output: action id, output name, declared logical path, type, size, digest. ACTION arguments
  (ids or unique prefixes, `match_action`) and `--step` filter the rows. Text mode is a table;
  `--json` follows the contract above.
- R2 The declared logical path of each output comes from the build's final plan
  (`build.json` → `final_plan`, else `plan`, read from the CAS), matched by output name; an
  output present in the result but missing from the plan is listed with `path: null`, never
  dropped.
- R3 `ebs fetch ACTION[.OUTPUT]…` materializes each named output (all outputs of the action
  when `.OUTPUT` is omitted; a STEP name selects all its actions) at
  `DIR/<action dir>/<declared logical path>` (default `DIR` = `./ebs-out`), files and trees
  alike, via `CAS.materialize` with `--mode` (default `copy`, so the result does not alias the
  CAS). `<action dir>` is the action id with `%` and `/` percent-encoded (`lint[block=alu]`
  stays readable; distinct ids never share a dir). `--flat` drops `<action dir>`. An argument
  is resolved as an action id/prefix or step name first; only if that fails is it split at its
  last `.` into ACTION and OUTPUT (row values may contain dots). Fetched files
  are read-only, as in the CAS. It prints one line per output: `<action_id>.<name> -> <dest>`.
- R4 Two requested outputs that resolve to the same `dest` (with `--flat`, e.g. `result.json`
  of every `sim[…]` row) are an error before anything is written, naming both actions and
  suggesting to drop `--flat`. An existing `dest` is an error unless `--force`, which removes it
  first; nothing outside `DIR` is ever written or removed (a logical path that escapes `DIR`
  after normalization is rejected).
- R5 Errors are actionable `EbsError`s with § 11 exit codes: unknown or ambiguous action
  (`match_action`); unknown output name (lists the action's output names); action without a
  result yet (states its current state; `infra_failed`, `skipped`, `cancelled`, still running);
  no `[metadata].url` configured; object missing from the CAS (says it may have been
  garbage-collected and suggests `ebs build --no-cache <step>`). A failure part-way leaves no
  partial `dest` (materialize already cleans up its own; earlier successful outputs stay).
- R6 Both commands only read: no cache writes, no build record or event changes, no CAS writes.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_outputs.py::test_lists_done_cached_and_failed`, `::test_filters_by_action_and_step`, `::test_json_shape` | R1 | unit |
| `test_outputs.py::test_path_from_final_plan`, `::test_output_not_in_plan_listed_with_null_path` | R2 | unit |
| `test_fetch.py::test_file_output_bytes_and_readonly`, `::test_tree_output`, `::test_all_outputs_when_name_omitted`, `::test_step_selects_all_actions`, `::test_action_dir_encoding`, `::test_flat`, `::test_mode_option` | R3 | unit |
| `test_fetch.py::test_flat_collision_writes_nothing`, `::test_existing_dest_needs_force`, `::test_path_escape_rejected` | R4 | unit |
| `test_fetch.py::test_errors` (parametrized: unknown action, ambiguous prefix, unknown output, no result, no metadata, missing blob) | R5 | unit |
| `test_fetch.py::test_read_only` (in-memory store + CAS snapshot unchanged) | R6 | unit |

Use the in-memory metadata store and a tmp-dir `FsCAS`, with a `SiteServices` subclass as the
Typer context object, as `tests/unit/cli/test_logs.py` does.

## Done when
- [x] `make check` passes (paste the summary line)
- [x] every R has a test; reviewer subagent reports no correctness gaps
- [x] interfaces.md § 14 updated; quickstart example passes `tests/e2e/test_docs.py`
- [x] Status set to `review` here and in docs/tasks/README.md

## Notes
- Raised by a user running `examples/lint-make`: after `ebs build` the per-block reports are only
  in the CAS (the runner deletes scratch), and `ebs logs` shows the tool's stdout, which happens
  to include the report only because that Makefile `cat`s it.
- `get_result` already returns the manifest for cached actions too (the driver records the hit's
  manifest per build), so no driver change is needed.
- Default `copy` keeps fetched files independent of the CAS; `hardlink`/`symlink` are for large
  trees and are the user's explicit choice.
- Implementation notes (P1-13): an output the plan does not declare (`path: null`) is fetched to
  its output name. Destinations that nest (a file output at `x/y` and a tree at `x`) count as a
  collision, like equal ones. A non-directory in the way below `DIR` is reported before anything
  is written. `-o` also has the long form `--output-dir`. `ebs-out/` is added to `.gitignore`.
  Shared fixture: `tests/helpers/outputs.py`.
- Follow-up for P1-09: `fetch` does not `touch` `last_access`; if GC must not evict objects that
  were just fetched, record that there.
- Follow-up: `ebs outputs` does not accept step names as ACTION arguments (the spec says
  `match_action`; `--step` covers it). `fetch` does, and the two could be aligned later.
