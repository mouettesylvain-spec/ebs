# P3-02 — `ebs discover` (strace)

Status: todo · Phase: 3 · Depends on: P3-01 · Size: M

## Goal
Help migrate coarse Makefile steps and explain sandbox failures by tracing which files a step actually reads.

## Read first
- docs/architecture.md § "Flow description format" → Legacy wrapping; § "Sandboxing phases" → Discovery and audit

## Scope (files)
- create `src/ebs/sandbox/discover.py`, `src/ebs/cli/discover.py`; tests `tests/unit/sandbox/test_discover.py`
  (parse fixture strace logs), `tests/integration/sandbox/test_discover_make.py`

## Requirements
- R1 `ebs discover STEP [--row k=v]` runs one instance under `strace -f -e trace=file,process -o …` (no sandbox),
  parses successful opens/execs/stat calls, and classifies paths: declared input, toolchain root, system, scratch
  output, **undeclared**.
- R2 Output: a proposed `inputs:` YAML patch (globs collapsed by directory), plus the list of undeclared reads with
  the first process that read each; never edits files unless `--write`.
- R3 Parser handles `-f` pid prefixes, unfinished/resumed syscall lines, `openat` with `AT_FDCWD` and relative dirfds.
- R4 Also usable as an audit (`--audit`) that exits non-zero when undeclared reads exist (for nightly jobs).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_discover.py::test_classification` | R1 | unit |
| `test_discover.py::test_patch_proposal_golden` | R2 | unit/golden |
| `test_discover.py::test_parser_edge_cases[...]` | R3 | unit |
| `test_discover_make.py::test_audit_exit_code` | R4 | integration |
