# P2-07 — GitLab CI integration

Status: todo · Phase: 2 · Depends on: P2-02 · Size: M

## Goal
CI pipelines run `ebs build --ci --cache=write`, show test results in merge requests and can gate channel
promotion.

## Read first
- docs/architecture.md § "Debugging, CLI, dashboard and CI" → CI

## Scope (files)
- create `src/ebs/ci/{gitlab,junit}.py`, `--ci` handling in `src/ebs/cli/build.py`, `examples/ci/.gitlab-ci.yml`
  template, `docs/user/ci.md`
- tests `tests/unit/ci/*`, `tests/e2e/test_ci_mode.py`

## Requirements
- R1 `--ci` implies `--cache=write`, `--rehash`, non-interactive output, CI QOS (P1-03), and records CI job
  metadata (`CI_PROJECT_PATH`, `CI_PIPELINE_ID`, `CI_JOB_URL`, commit) in the build.
- R2 JUnit XML: one testcase per sim action (classname = step, name = test[seed]), failure message = first error
  signature, system-out = log tail (bounded), cached results marked with a property; validates against the
  JUnit XSD used by GitLab.
- R3 Writes a build link and summary into `$CI_PROJECT_DIR/ebs-summary.md` and dotenv artifact `EBS_BUILD_ID`.
- R4 `ebs channel set --if-passing` usable as a pipeline gate job (exit non-zero if gate fails).
- R5 CI runs under a per-team service account token (bearer auth from P1-06); the token never appears in logs.

## Tests
| Test | Covers | Kind |
| --- | --- | --- |
| `test_gitlab.py::test_ci_defaults`, `::test_metadata_recorded` | R1 | unit |
| `test_junit.py::test_schema_valid`, `::test_mapping` | R2 | unit |
| `test_gitlab.py::test_summary_files` | R3 | unit |
| `tests/e2e/test_ci_mode.py::test_gate` | R4 | e2e |
| `test_gitlab.py::test_token_redacted` | R5 | unit |
