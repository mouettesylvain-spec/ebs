# P2-07 — GitLab CI integration

Status: done · Phase: 2 · Depends on: P2-02 · Size: M

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

## Notes
- Dependency added (dev only): `xmlschema`, to validate the JUnit report against an XSD (R2); no
  XSD validator was available (`lxml` is not a dependency). The schema,
  `tests/fixtures/ci/junit.xsd`, follows the Jenkins `junit-10.xsd` structure GitLab's parser
  reads, plus testcase `properties` (for `ebs.cached`).
- P2-02 was `review` (merged to main), treated as done.
- R1 CI QOS: `ebs build` still only offers the local executor, so `--ci` passes
  `build_kind="ci"` and `project` to `SiteServices.open_executor`; whoever wires the SLURM
  executor into `ebs build` (P1-12/P2-08) must forward them to `SlurmExecutor`.
- R1 metadata: stored in the `build_started` event (`ci`), `build.json` (`ci`) and the existing
  `ci_job` column (job URL); no schema migration. CONTRACT CHANGE: `run_build(ci=…)`.
- "Sim action" = any action with a `test` param (classname = step, name = `test[seed]`).
- R4: `--if-passing` reads `--gate-build`, else `$EBS_BUILD_ID`. A failed gate exits 2 like the
  existing P2-02 gate errors (`ReleaseError`). Through the HTTP metadata service the stored gate
  record is the service's (its own policy); the "build passed" check runs in the CLI.
- R5: masked are the values of `EBS_META_TOKEN`, `CI_JOB_TOKEN`, `CI_REGISTRY_PASSWORD`,
  `CI_DEPLOY_PASSWORD`, `CI_JOB_JWT(_V2)`; the content of `[metadata].token_file` is not (it
  is never printed). Follow-up: mask it too if a tool could print it.
- Follow-up: the summary's build link needs `[ci].build_url` until the dashboard (P2-06) defines
  build URLs.
