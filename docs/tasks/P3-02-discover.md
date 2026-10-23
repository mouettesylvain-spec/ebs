# P3-02 — `ebs discover` (strace)

Status: done · Phase: 3 · Depends on: P3-01 · Size: M

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

## Notes
- No new dependency (ruamel.yaml, already allowed, renders the patch). `ebs.sandbox.discover`
  imports only `core` and `ebs.sandbox.bwrap` (sandbox-minimal contract); the runner never
  imports it.
- "No sandbox" is read as no bwrap/Apptainer: the instance is staged with the runner's own
  `build_sandbox` (strict staging, as `ebs reproduce`), so declared inputs and upstream outputs
  sit at their logical paths and absolute reads succeed and show up in the trace. A failed read
  inside the work dir of a file that exists in the flow directory is reported as undeclared
  (`missing`: "not staged"); that is how a Makefile's `../common/x.mk` is found.
- strace gets `-y` (fd paths, so `AT_FDCWD`/dirfds resolve without tracking) and `-s 65535`
  (no truncated paths) besides the required `-f -e trace=file,process -o`; logs without `-y`
  still parse (per-pid cwd/fd/program tracking across chdir and fork).
- `system` = what bwrap shows of the OS plus `/sys` (not mounted by bwrap, but glibc/OpenMP
  probes there would otherwise make every audit fail); reads in `/tmp` are undeclared.
- CI: the integration job installs strace and sets `EBS_REQUIRE_STRACE=1`; elsewhere the
  integration tests skip without strace. Extra hermetic CLI tests (fake strace):
  tests/unit/cli/test_discover.py.
- Follow-ups: a nightly `ebs discover --audit` job per flow (deploy/ cron or CI template);
  `--row` picks the first match only, so an audit over every matrix row needs a loop or an
  `--all-rows` option; a path containing `${` would need template escaping in the patch.
- Review outcome: the trace also follows `fchdir` (`-e trace=file,process,fchdir`); `--audit`
  fails on an incomplete trace (the tool failed or timed out), and such a trace never drops or
  narrows declared inputs (`partial`); SIGTERM removes the scratch (exit 130).
- Known limits, left for a follow-up (test-critic): failed probes *outside* the work dir (e.g.
  a missing `~/.lintrc`) are not reported; anything under the domain CAS root counts as `input`;
  `/proc/self/root/…` and `/proc/self/fd/N` count as `system`; directory listings are not reads
  (getdents is not traced); a path-syscall line the parser cannot split is skipped silently
  rather than failing the audit.
