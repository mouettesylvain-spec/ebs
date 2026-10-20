# P3-01 — bubblewrap sandbox (+ Apptainer fallback)

Status: review · Phase: 3 · Depends on: P1-04 · Size: L

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

## Notes
- D6: default confirmed by the user (2026-10-20): `auto` = bwrap, else Apptainer (only with
  `[sandbox].apptainer_image`), else staging only with a warning on stderr. An explicit `bwrap`
  the node cannot run is an infra failure (exit 75), never a silent downgrade.
- No new dependency. `runner` may now import `ebs.sandbox` (.importlinter); `sandbox` imports
  only `core` and `flow` (new `sandbox-minimal` contract).
- Contract changes (interfaces.md § 4, § 9a): `ActionSpec.sandbox`, `PlanToolchain.install_roots`
  (both written to plan.json only when set; no key change, golden keys unchanged, golden plan
  gains them), `RunnerInfo.sandbox`, `[sandbox]` site config. web/openapi.json and the dashboard
  types were regenerated for `RunnerInfo.sandbox`.
- Integration tests skip without bwrap; the grid nightly runs them with `EBS_REQUIRE_SANDBOX=1`
  (.gitlab-ci.yml, .github/workflows/nightly.yml), which turns the skip into a failure. Verified
  locally against bubblewrap 0.9.0 (6 passed), and test_questa_fake_flow under bwrap (5 passed).
- Follow-ups:
  - `ebs reproduce` (P1-10) still builds the sandbox without bwrap; reproducing a bwrap failure
    needs the same wrap (P3-02 or a small task).
  - The runner never configures structlog, so its log lines go to stdout and would corrupt the
    manifest printed without `--build`; sandbox messages use stderr for that reason. Configure
    runner logging to stderr.
  - Install roots that are symlinks are bound at the link path with the target's contents;
    tools that resolve their own real path (`readlink -f $0`) may need the target in `ro_binds`.
  - The whole domain CAS dir is mounted read-only (large inputs link into it); binding only the
    staged blobs would be tighter.
  - Apptainer fallback needs Apptainer >= 1.1 (`--no-mount bind-paths`); not exercised on a real
    Apptainer here.
  - Flip `sandbox:` default to `auto` for more kinds once teams have run `ebs discover` (P3-02).
