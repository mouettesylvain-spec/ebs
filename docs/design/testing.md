# Testing strategy

Every feature ships with tests in the same commit. A task is done only when each of its
requirements (R1…Rn) is covered by a test that would fail if the requirement were broken.

## Layers

| Layer | Marker | Where | Runs | Budget |
| --- | --- | --- | --- | --- |
| Unit | `unit` (auto-applied under tests/unit) | tests/unit/ mirrors src/ebs/ | every commit, `make check` | < 1 s per test, whole suite < 60 s |
| Property | `unit` + Hypothesis | next to the unit tests | every commit (`max_examples=200`), nightly (`=5000`) | |
| Perf | `unit` + `perf` | next to the unit tests | every commit, `make test-perf` (no coverage: tracing slows code 2-3x) | the budget the test asserts; a `perf` test run under coverage fails on purpose |
| Integration perf | `integration` + `perf` | tests/integration/ | every MR, `make test-perf-integration` and CI's "Integration timing budgets" step: alone, never among other tests (shared CPUs made them flaky) | the budget the test asserts |
| Contract | `unit` or `integration` | tests/contract/ | every commit | one suite per protocol, parametrized over all implementations |
| Integration | `integration` | tests/integration/ | every MR (`make check-all`) | < 10 min total |
| Golden | `unit` | tests/golden/ with syrupy snapshots | every commit | snapshot updates need a reason in the commit body |
| End-to-end | `integration` | tests/e2e/ | every MR | runs `ebs` as a subprocess on examples/ with local or fake-SLURM executor |
| Grid | `grid` | tests/grid/ | nightly on a real SLURM partition | |
| Vendor | `vendor` | tests/vendor/ | nightly on nodes with Questa/SpyGlass licenses | |

`tests/conftest.py` fails the run if an `integration` test is collected but its dependency is
missing in CI (`CI=true`); locally it skips with a clear message. Unit tests must not touch the
network, real `$HOME`, `/tmp` outside `tmp_path`, or wall-clock time (use `ebs.core.clock.FakeClock`).

## Fakes (shared, in `tests/fakes/` or `tests/helpers/` — each built by the task that first needs it)

- **InMemoryMetadataStore** (`ebs.meta.memory`, shipped code, not test-only): must pass the
  MetadataStore contract suite. (P0-10)
- **tmp CAS**: fixture `cas(tmp_path, domain="test")` returning an `FsCAS`. (P0-09)
- **FakeS3** (`tests/helpers/s3.py`): in-memory, thread-safe stand-in for the boto3 S3 client
  calls `S3CAS` makes; raises botocore `ClientError`s with real codes, checks `ChecksumSHA256`
  and the 5 MiB minimum part, records calls, takes per-operation hooks for fault injection, and
  can play a server without conditional writes. Session fixture `s3_server` gives a real
  S3-compatible endpoint: `EBS_TEST_S3_URL` (+ `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) or a
  SeaweedFS container (MinIO no longer publishes images). (P3-07)
- **ScriptedExecutor** (`tests/helpers/driver.py`): an in-process Executor playing the runner's
  part (loads the plan from the CAS, checks keys and producers, posts results, caches in `write`
  mode) with outcomes scripted per attempt; plus `make_plan` for hand-built DAGs and
  `SleepRecorder`. (P0-15)
- **Fake tools** (`tests/fakes/bin/`): small Python scripts named `vlog`, `vopt`, `vsim`, `vlib`,
  `vmap`, `spyglass`, `make`-compatible Makefiles. They reproduce the *observable behaviour* the
  rules depend on: argv parsing, output files, transcript format (UVM report summary lines,
  `** Error:` lines, license-checkout failure messages), exit codes, embedded timestamps (to
  exercise `deterministic: false`). Behaviour is selected by argv/env (`FAKE_QUESTA_MODE=license_fail`).
  They contain no vendor text beyond generic message shapes. (P1-07)
- **FakeSlurm** (`tests/fakes/slurm/`): executables `sbatch`, `squeue`, `sacct`, `scancel`,
  `scontrol` put first on `PATH`. Backed by a SQLite state file; runs jobs as local subprocesses
  with a worker pool; supports `--array` with `%N`, `-L` license pools (per-test config),
  `--mem` enforcement (kills with OOM state), `--time`, `--account/--qos` recording, and fault
  injection (`NODE_FAIL`, `PREEMPTED`, stuck `PENDING (Licenses)`). Its `--json` output is
  validated against JSON captured from SLURM 26.05 (`tests/fixtures/slurm/26.05/*.json`). (P1-01)
  `FakeSlurm.install(dir)` writes the commands to `dir/bin` (pass `SlurmCli(bin_dir=…)` or put it
  on PATH); there is no daemon: each command and each job's detached supervisor runs a scheduling
  pass under a file lock. Config (`configure(cpus=, licenses=, accounts=, qos=, time_scale=,
  min_job_age_s=, …)`) and faults (`inject({"fault": "NODE_FAIL" | "PREEMPTED" | "STUCK_LICENSES"
  | "CLI_ERROR", …})`) are JSON files in its state dir, documented in `tests/fakes/slurm/core.py`.
  Fixture `fake_slurm` (tests/integration/slurm/conftest.py) cleans up running jobs at teardown.
  Supervisors are forked from the (single-threaded) command or supervisor that started the job,
  after its database transaction closed, and the package `__init__` stays import-light: a
  2,000-task fake regression runs in well under a minute on 4 cores (P1-02).
  `tests/helpers/slurm.py::ScriptedSlurm` is an in-process `CommandRunner` for executor unit
  tests that need no real job processes.
  The 26.05 fixtures were hand-written in P1-01; `scripts/capture_slurm_fixtures.py` captures and
  anonymizes real ones.
- **Postgres**: `EBS_TEST_PG_URL` if set, otherwise a `testcontainers` PostgreSQL 17 container per
  session; each test gets its own schema via a transaction/rollback or `CREATE SCHEMA` fixture.
- **Git repos**: fixture `git_repo(tmp_path)` that creates a repo with commits; never use this repo.

## Rules for writing tests

1. Name tests after behaviour: `test_cache_hit_skips_submission`, not `test_driver_3`.
2. Map tests to requirements: put `# R3` on the line above the test (the reviewer checks coverage).
3. Don't mock the unit under test or its immediate collaborators when a fake exists; mock only at
   process/network boundaries (`subprocess`, `httpx` via respx, SLURM via FakeSlurm).
4. Every bug fix starts with a failing regression test.
5. Property tests for anything that hashes, canonicalizes, orders, or parses user input.
6. Error paths are requirements too: test the message content for user-facing errors
   (file:line:col for flow errors, a "did you mean" for unknown refs).
7. Tests must be deterministic: seed Hypothesis in CI (`derandomize=True` on PRs), no sleeps —
   use FakeClock or poll-with-timeout helpers from `tests/helpers/wait.py`.

## Coverage gates (`pyproject.toml` / CI)

- Overall line coverage ≥ 85 %, branch coverage on.
- `ebs/core`, `ebs/plan`, `ebs/cas`, `ebs/sources`, `ebs/gc`: ≥ 95 % line, ≥ 90 % branch
  (checked per package by `scripts/coverage_gate.py`).
- New code in a task must not reduce a package's coverage.

## CI pipeline (GitHub Actions, `.github/workflows/`)

Tuned for a small CI budget. Runs on pull requests and pushes to `main` only; docs-only changes
(`docs/**`, `*.md`, `.claude/**`) are skipped and a newer push cancels the superseded run.

`lint` (ruff, mypy against 3.11, import-linter, 3.11 import smoke test) → `unit` (py3.13,
`unit and not slow`, `-x`, coverage gates) → `full`, on ready-for-review PRs and `main` only:
slow unit tests, `integration` (Postgres service), `e2e` (examples with local + fake SLURM).
`nightly.yml` (manual until P1-12): `perf` budgets, `grid`, `vendor`, Hypothesis long run,
mutmut on the invariant files (invariants.md). `.gitlab-ci.yml` is the legacy GitLab pipeline
and is no longer maintained.
