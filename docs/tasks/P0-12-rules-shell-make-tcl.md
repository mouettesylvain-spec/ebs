# P0-12 — Rule plugin API and `shell`, `make`, `tcl` rules

Status: review · Phase: 0 · Depends on: P0-05 · Parallel-safe with: P0-09 · Size: M

## Goal
A plugin interface for step kinds, plus the three generic rules that let teams wrap existing Makefile
and Tcl flows on day one.

## Read first
- docs/architecture.md § "Flow description format" → Concepts (Step, `kind`) and "Legacy wrapping"
- docs/design/interfaces.md §7

## Scope (files)
- create `src/ebs/rules/api.py`, `registry.py` (entry-point discovery, group `ebs.rules`), `shell.py`,
  `make.py`, `tcl.py`
- register built-ins in `pyproject.toml` entry points
- create `tests/unit/rules/test_registry.py`, `test_shell.py`, `test_make.py`, `test_tcl.py`

## Out of scope
Questa rules (P1-08). Discovery via strace (P3-02).

## Contract
interfaces.md §7. `ActionTemplate` = argv, extra implicit inputs (e.g. the Makefile itself), implicit
outputs, config files, default `classify` behaviour.

## Requirements
- R1 Registry loads plugins by entry point, rejects duplicate kinds, and lists them (`ebs rules list` in P0-16);
  a plugin whose `version` is not a non-empty string fails registration.
- R2 `shell`: `script:` (string) runs via `bash --noprofile --norc -eo pipefail script.sh`, where the script
  is written as a config file (so its content is hashed); or `command:` (list) runs as argv. Exactly one of them.
- R3 `make`: argv `make -C <workdir> <target> [VAR=value…]` with `params` mapped to make variables in sorted
  order; the whole `workdir` tree and declared input globs are inputs; declared output dirs are outputs;
  `MAKEFLAGS`/`MFLAGS`/`MAKELEVEL` are removed from env; `-j` comes from `resources.cpus` but is **not** part
  of the key (passed via env `EBS_CPUS` and `MAKEFLAGS=-j$EBS_CPUS` set by the runner).
- R4 `tcl`: argv `<tclsh-or-tool> <script> [args]` with `params` exported as a generated `ebs_params.tcl`
  (`set ::ebs(name) {value}` with Tcl-safe quoting, tested against a real `tclsh` if available, else skipped
  with a reason) sourced before the user script.
- R5 Default `classify`: exit 0 ⇒ PASSED; non-zero ⇒ FAILED; signals SIGKILL/SIGSEGV/SIGBUS ⇒ INFRA("tool_crash");
  log tail matching configurable license patterns (`[rules.license_error_patterns]`) ⇒ INFRA("license").
- R6 Changing a rule's generated argv requires bumping its `version`; a golden test per rule fixes the argv
  for a reference step (so accidental changes fail the test with a hint to bump the version).

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `test_registry.py::test_entry_points`, `::test_duplicate_kind`, `::test_bad_version` | R1 | unit |
| `test_shell.py::test_script_is_config_file`, `::test_command_argv`, `::test_exactly_one` | R2 | unit |
| `test_make.py::test_argv_and_vars_sorted`, `::test_jobs_not_in_key`, `::test_makeflags_scrubbed` | R3 | unit |
| `test_tcl.py::test_params_file_quoting` (Hypothesis on strings incl. braces, `$`, `[`), `::test_real_tclsh` | R4 | unit/property |
| `test_*::test_classify[...]` | R5 | unit |
| `test_*::test_golden_argv` | R6 | unit/golden |

## Done when
- [x] `make check` passes

## Notes
- Contract: interfaces.md §7 now defines `ExpandContext`, `ActionTemplate`, `Classification`,
  `RuleSettings`, `RuleRegistry` and helpers (they were named but undefined).
- `ActionTemplate.runtime_env` (make: `MAKEFLAGS=-j$EBS_CPUS`) is not part of the key. **P0-08** must
  carry it in a non-key `ActionSpec` field, and **P0-13** must expand it with `expand_runtime_env`
  after setting `EBS_CPUS`. P0-08 also merges the template's implicit inputs/config files with the
  declared ones and must reject name/path collisions (generated files live under `.ebs/`).
- `Classification` reason `"tool_crash"` (R5) is not in `meta.api.InfraReason` (P0-10). **P0-13**
  must map it, or meta should add `tool_crash`.
- `[rules].license_error_patterns` is defined in `RuleSettings`, but nothing loads it from
  `ebs.toml` yet (`ebs.config` is a stub). It gets wired up where the registry is built (P0-16).
- Default classify also treats exit codes 128+N (a shell or make reporting that a child was
  killed by SIGKILL/SIGSEGV/SIGBUS) as a crash. The trade-off: a tool that genuinely exits
  135/137/139 gets retried instead of cached, which is better than caching a crash as a test failure.
- Tcl quoting is property-tested against real Tcl 9.0 via stdlib `tkinter.Tcl()`.
  `test_real_tclsh` skips when there is no `tclsh` on PATH.
- Follow-ups: the tcl argv shape `<tool> <script> [args]` cannot express `dc_shell -f x.tcl` or
  `vsim -do x.tcl` (vendor packs, P1-08, or a script placeholder); tcl does not add the user script
  as an implicit input (it must be declared; strict staging makes an omission fail loudly).
- P0-05 was merged to main (5cbfd41) while the README still said `review`; treated as done.

