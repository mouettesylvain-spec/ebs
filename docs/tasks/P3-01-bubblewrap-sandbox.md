# P3-01 — bubblewrap sandbox (+ Apptainer fallback)

Status: todo · Phase: 3 · Depends on: P1-04 · Size: L

## Goal
Undeclared reads fail loudly instead of silently producing wrong cache hits: tools run in a mount namespace
that contains only scratch, declared inputs, read-only toolchain roots and system dirs.

## Read first
- docs/architecture.md § "Sandboxing phases" (v2 bubblewrap), § "Risks" (user namespaces); open decision D6

## Scope (files)
- create `src/ebs/sandbox/{api,staging,bwrap,apptainer,detect}.py`; runner uses `Sandbox` protocol
- tests `tests/unit/sandbox/*` (argv golden), `tests/integration/sandbox/*` (skipped with reason if userns unavailable;
  mandatory in the grid nightly)

## Requirements
- R1 `Sandbox` protocol: `wrap(argv, mounts, env, cwd) -> argv`; implementations: `staging` (v1 no-op), `bwrap`,
  `apptainer`; `detect()` picks per D6 and reports why.
- R2 bwrap profile: `--unshare-all --share-net` (network needed for FlexLM), `--die-with-parent`, ro-bind `/usr`,
  `/lib*`, `/etc` subset (passwd, group, hosts, resolv.conf, nsswitch, ld.so.*), toolchain install roots ro, CAS
  inputs ro, scratch rw, private `/tmp`, `/proc`, `/dev` minimal.
- R3 An undeclared read (e.g. `$HOME/.synopsys_dc.setup`, a project NFS path) fails with ENOENT; the runner
  detects likely sandbox failures in the log and adds a hint pointing to `ebs discover`.
- R4 Per-flow opt-in/opt-out (`sandbox: bwrap|staging|auto`); default `auto` for Questa kinds at the end of phase 3.
- R5 The sandbox choice is recorded in the result manifest but not in the key (same inputs ⇒ same key).

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_detect.py::test_selection[...]` | R1 | unit |
| `test_bwrap.py::test_argv_golden` | R2 | unit |
| `tests/integration/sandbox/test_undeclared_read_fails.py`, `test_hint.py` | R3 | integration |
| `test_config.py::test_opt_in_out` | R4 | unit |
| `tests/unit/plan/test_key_sensitivity.py` (sandbox excluded) | R5 | property |
