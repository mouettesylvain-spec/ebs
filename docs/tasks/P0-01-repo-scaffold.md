# P0-01 — Repository scaffold and quality gates

Status: todo · Phase: 0 · Depends on: — · Parallel-safe with: — · Size: M

## Goal
A clean, installable `ebs` package with every quality gate wired, so all later tasks only add code
and tests. After this task `make check` runs ruff, mypy --strict, import-linter and pytest.

## Read first
- docs/design/overview.md (package map, layering, dependencies)
- docs/design/testing.md (markers, coverage gates, CI pipeline)
- docs/design/invariants.md I17, I18

## Scope (files)
- `pyproject.toml` (hatchling build, `uv` workflow, Python ≥ 3.11, extras `[dev]`, `[blake3]`, `[s3]`),
  console scripts `ebs = ebs.cli.main:app`, `ebs-runner = ebs.runner.main:main` (stub)
- `src/ebs/__init__.py` (`__version__` from package metadata), empty sub-packages from the package map
  each with a one-line module docstring
- `src/ebs/core/errors.py` (hierarchy from interfaces.md §11), `src/ebs/core/log.py`, `src/ebs/core/clock.py`
  (`Clock` protocol, `SystemClock`, `FakeClock`)
- `src/ebs/cli/main.py`: Typer app with `ebs --version` only
- `Makefile` targets: `check`, `check-all`, `fmt`, `cov`, `nightly`
- `.importlinter` contracts for the layering in overview.md
- `tests/conftest.py` (auto markers by directory, CI-strict skipping, `FakeClock` fixture),
  `tests/helpers/wait.py`, `tests/unit/test_layering.py`, `tests/unit/test_no_shell_true.py`
- `scripts/coverage_gate.py` (per-package thresholds from testing.md)
- `.gitlab-ci.yml` (stages from testing.md; nightly jobs defined but `when: manual` until P1-12)
- `.pre-commit-config.yaml` (ruff, ruff-format, mypy on staged files)
- `LICENSE` (Apache-2.0), `NOTICE`, `CONTRIBUTING.md` (DCO sign-off, how to run tests), `README.md` stub
- keep the agent kit files as-is (CLAUDE.md, .claude/, docs/)

## Out of scope
Any functional code. The CLI has only `--version`.

## Requirements
- R1 `uv sync --all-extras && uv run ebs --version` prints the package version.
- R2 `make check` runs ruff check, ruff format --check, mypy --strict on src/ and tests/,
  lint-imports, and `pytest -m unit` with coverage; it fails if any of them fails.
- R3 Tests under `tests/unit` get the `unit` marker automatically, `tests/integration` get
  `integration`, etc.; `pytest --strict-markers` rejects unknown markers.
- R4 When `CI=true`, an integration test whose dependency is missing fails instead of skipping.
- R5 `test_no_shell_true.py` fails if any file under src/ calls `subprocess.*` or `asyncio.create_subprocess_shell`
  with `shell=True` (AST-based, not regex); an allowlist comment `# ebs: allow-shell <reason>` is
  supported but must be empty today.
- R6 `test_layering.py` runs import-linter programmatically and fails on a violation (prove it with a
  temporary violating module created in `tmp_path` on `sys.path`).
- R7 `scripts/coverage_gate.py` exits non-zero when a listed package is under its threshold (unit-test it
  with a synthetic coverage JSON).
- R8 `FakeClock` supports `now()`, `monotonic()`, `advance(seconds)` and `sleep()` that advances instead of waiting.

## Tests (write these first)
| Test | Covers | Kind |
| --- | --- | --- |
| `tests/e2e/test_cli_version.py::test_version_matches_metadata` (subprocess) | R1 | integration |
| `tests/unit/test_markers.py::test_auto_marker_by_directory` (pytester) | R3 | unit |
| `tests/unit/test_markers.py::test_ci_strict_missing_dependency_fails` (pytester) | R4 | unit |
| `tests/unit/test_no_shell_true.py::test_src_has_no_shell_true`, `::test_detector_catches_positive_samples` | R5 | unit |
| `tests/unit/test_layering.py::test_contracts_hold`, `::test_violation_detected` | R6 | unit |
| `tests/unit/scripts/test_coverage_gate.py` | R7 | unit |
| `tests/unit/core/test_clock.py` | R8 | unit |
| R2 is verified by running `make check` with an intentionally failing ruff sample in a scratch copy (document the command in the MR) | R2 | manual evidence |

## Done when
- [ ] `make check` passes on a fresh clone
- [ ] CI pipeline file validates (`gitlab-ci-lint` not required; YAML parses and stages match testing.md)
- [ ] Status updated

## Notes
- D3: GitLab CI by default.
- mypy strict settings: `disallow_any_explicit = false` (Pydantic), everything else strict.
