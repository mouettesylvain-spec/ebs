# EBS — EDA Build System

Hash-based build orchestrator that runs EDA flows (Questa, lint, later Synopsys) on SLURM with a
shared content-addressed cache, team releases and provenance. Python 3.11+, Apache-2.0, open source.

- Architecture (the "why"): @docs/architecture.md — read the relevant section, not the whole file.
- Module map, contracts, invariants: docs/design/ (start with docs/design/overview.md).
- Work items: docs/tasks/README.md (index + dependency graph). One task file = one session.

## Commands

- `uv sync --all-extras` — install dev env
- `make check` — ruff + mypy --strict + unit tests (must pass before every commit)
- `make check-all` — adds integration tests (needs Postgres: `EBS_TEST_PG_URL` or Docker)
- `uv run pytest tests/unit/core/test_digest.py -q` — prefer running single test files while iterating
- `uv run pytest -m "unit and not slow" -x -q` — fast loop
- `uv run ebs --help` — the CLI
- Test markers: `unit` (default, hermetic, <1 s each), `integration` (Postgres/fake SLURM),
  `grid` (real SLURM, nightly only), `vendor` (needs EDA licenses, nightly only), `perf`
  (wall-clock budgets: `make test-perf`, never under `--cov`). Never run `grid`/`vendor`
  locally unless the task says so.

## Workflow for every task

1. Run `/implement-task <ID>` (skill in .claude/skills). It reads docs/tasks/<ID>*.md.
2. Explore only the files the task lists. Use a subagent for anything broader.
3. Write the tests from the task's "Tests" section first. Run them and show they fail.
4. Implement until `make check` passes. Show the passing output as evidence.
5. Run the `task-reviewer` subagent on the diff; fix correctness gaps only.
6. Update the task's Status line and docs/tasks/README.md; commit.

IMPORTANT: every requirement (R1, R2…) in a task needs at least one test that fails if the
requirement is broken. Do not weaken, skip or delete an existing test to make it pass; if a test
is wrong, say why in the commit message.

## Code conventions

- Package root `src/ebs/`; tests mirror it under `tests/unit/` and `tests/integration/`.
- `mypy --strict` clean; no `Any` in public signatures; `from __future__ import annotations`.
- Public cross-package contracts live in `api.py` / `types.py` files and are documented in
  docs/design/interfaces.md. Changing one needs the doc updated in the same commit and a note in
  the commit body ("CONTRACT CHANGE: …").
- Pure functions for planning/hashing; I/O at the edges. Inject clocks, filesystems roots,
  subprocess runners and executors — no module-level singletons.
- Subprocesses: argv lists only, never `shell=True` with interpolated strings.
- Errors: raise subclasses of `ebs.core.errors.EbsError` with an actionable message
  (what failed, which file/step, what to do). CLI maps them to exit codes.
- Logging via `structlog`-style `ebs.core.log.get_logger(__name__)`; no `print` outside `cli/`.
- Dependencies: only those listed in docs/design/overview.md § Dependencies. Adding one needs
  a line in the task's notes explaining why.

## Invariants that must never break (see docs/design/invariants.md)

- IMPORTANT: anything that changes action-key inputs or canonical JSON output requires bumping
  `ebs.plan.keys.KEY_SCHEMA_VERSION` and updating the golden key fixtures.
- Rerun decisions compare content digests, never timestamps.
- CAS objects are immutable; writes go tmp/ → fsync → atomic rename.
- Cache lookups and CAS paths are always scoped by domain.
- No vendor data, PDK content, license server names or real hostnames in the repo or fixtures.

## Git

- Branch `task/<ID>-<slug>`; Conventional Commits (`feat(plan): …`); DCO sign-off: `git commit -s`.
- Never push, never force-push, never rewrite shared history. The human opens the MR.

## When compacting

Preserve: the task ID, the list of requirements still untested, files modified, and the exact
test command in use.
