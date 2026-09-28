# Contributing to EBS

Thanks for helping. EBS is Apache-2.0 licensed, and contributions are accepted under the same license.

## Developer Certificate of Origin

Every commit must be signed off ([DCO](https://developercertificate.org/)). The sign-off certifies
that you wrote the change, or otherwise have the right to submit it:

```sh
git commit -s -m "feat(plan): compute action keys"
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat(scope): …`, `fix(scope): …`, `docs: …`, `test: …`, `chore: …`).

## Setting up

You need Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --all-extras         # create .venv with runtime, dev and optional extras
uv run ebs --help
uvx pre-commit install       # optional: ruff + mypy on staged files
```

## Running the checks

| Command | What it runs |
| --- | --- |
| `make check` | ruff, ruff format --check, mypy --strict, import-linter, unit tests with coverage gates. Must pass before every commit |
| `make check-all` | `make check` plus integration and e2e tests (Postgres through `EBS_TEST_PG_URL`, or Docker) |
| `make fmt` | format and auto-fix |
| `make cov` | unit + integration with an HTML coverage report in `htmlcov/` |
| `uv run pytest tests/unit/core/test_clock.py -q` | one test file (fastest loop) |

Tests get their marker from their directory: `tests/unit` → `unit`, `tests/integration` and
`tests/e2e` → `integration`, `tests/grid` → `grid`, `tests/vendor` → `vendor`. `grid` and `vendor`
tests need a real SLURM partition or EDA licenses and run nightly only.

When a test needs a missing dependency, skip it with
`pytest.skip("missing dependency: <what and how to provide it>")`. Locally that is a skip. With `CI=true` an
integration or e2e test fails instead, so CI never silently loses coverage.

See docs/design/testing.md for the full strategy and CLAUDE.md for code conventions.

## Hygiene

- No vendor data, PDK content, license server names or real hostnames in code, tests or fixtures.
- No `shell=True` subprocesses (enforced by `tests/unit/test_no_shell_true.py`).
- Package layering is enforced by import-linter (`.importlinter`, docs/design/overview.md).
