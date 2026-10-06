"""Shared test configuration: markers by directory, CI-strict dependency handling, fixtures.

Kept free of imports from `tests.*` so tests/unit/test_markers.py can reuse it in pytester runs.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Generator
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock

MARKERS = {
    "unit": "hermetic, < 1 s each; runs on every commit (default under tests/unit)",
    "integration": "needs Postgres, fake SLURM or subprocesses; runs on every MR",
    "grid": "needs a real SLURM partition; nightly only",
    "vendor": "needs EDA tool licenses; nightly only",
    "slow": "slower than the unit budget; excluded from the fast loop",
    "perf": "asserts a wall-clock budget; runs in `make test-perf`, never under coverage tracing",
}

PERF_UNDER_COVERAGE = (
    "perf tests assert wall-clock budgets and coverage tracing slows code down 2-3x, which makes "
    "them flaky: run them with `make test-perf` (no --cov), and exclude them from coverage runs "
    "with -m 'not perf'"
)

# First directory under tests/ -> marker applied to every test in it.
DIRECTORY_MARKERS = {
    "unit": "unit",
    "golden": "unit",
    "integration": "integration",
    "e2e": "integration",
    "grid": "grid",
    "vendor": "vendor",
}
# Tests here choose their own layer; unmarked ones default to `unit`.
DEFAULT_UNIT_DIRECTORIES = {"contract"}
LAYER_MARKERS = {"unit", "integration", "grid", "vendor"}

# Skip reasons that mean "a dependency is missing". Tests use
# `pytest.skip("missing dependency: <what and how to provide it>")` or `pytest.importorskip`.
MISSING_DEPENDENCY_PREFIXES = ("missing dependency:", "could not import")
CI_STRICT_DIRECTORIES = {"integration", "e2e"}

_TESTS_ROOT = Path(__file__).resolve().parent


def pytest_configure(config: pytest.Config) -> None:
    for name, help_text in MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {help_text}")
    try:
        from hypothesis import HealthCheck, settings
    except ImportError:  # pragma: no cover
        return
    settings.register_profile("dev", max_examples=200)
    settings.register_profile("ci", max_examples=200, derandomize=True)
    settings.register_profile(
        "nightly", max_examples=5000, suppress_health_check=[HealthCheck.too_slow]
    )
    default = "ci" if _in_ci() else "dev"
    settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", default))


def _in_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"1", "true", "yes"}


def _top_directory(path: Path) -> str | None:
    try:
        parts = path.resolve().relative_to(_TESTS_ROOT).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def _coverage_active() -> bool:
    try:
        from coverage import Coverage
    except ImportError:  # pragma: no cover - dev extra always installs it
        return False
    return Coverage.current() is not None


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("perf") is not None and _coverage_active():
        pytest.fail(PERF_UNDER_COVERAGE, pytrace=False)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        top = _top_directory(item.path)
        marker = DIRECTORY_MARKERS.get(top or "")
        if marker is None and top in DEFAULT_UNIT_DIRECTORIES:
            has_layer = any(m.name in LAYER_MARKERS for m in item.iter_markers())
            marker = None if has_layer else "unit"
        if marker is not None:
            item.add_marker(marker)
    unlayered = [
        item.nodeid
        for item in items
        if not any(m.name in LAYER_MARKERS for m in item.iter_markers())
    ]
    if unlayered:
        # Such tests would be selected by neither `-m unit` nor `-m integration` and never run.
        raise pytest.UsageError(
            "tests without a layer marker (put them under tests/unit, tests/integration, ... "
            f"or mark them with one of {sorted(LAYER_MARKERS)}): " + ", ".join(unlayered)
        )


def _is_missing_dependency(longrepr: object) -> str | None:
    # Skip reports carry (path, lineno, reason).
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        reason = str(longrepr[2]).removeprefix("Skipped: ")
        if reason.lower().startswith(MISSING_DEPENDENCY_PREFIXES):
            return reason
    return None


def _escalate(
    report: pytest.TestReport | pytest.CollectReport, path: Path, *, integration: bool = False
) -> None:
    strict = integration or _top_directory(path) in CI_STRICT_DIRECTORIES
    if not (report.skipped and _in_ci() and strict):
        return
    reason = _is_missing_dependency(report.longrepr)
    if reason is None:
        return
    report.outcome = "failed"
    report.longrepr = (
        f"CI=true and an integration dependency is missing, so this fails instead of "
        f"skipping: {reason}"
    )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, None]:
    del call
    outcome = yield
    is_integration = item.get_closest_marker("integration") is not None
    _escalate(outcome.get_result(), item.path, integration=is_integration)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, None]:
    outcome = yield
    _escalate(outcome.get_result(), collector.path)


@pytest.fixture
def fake_clock() -> FakeClock:
    """A FakeClock at 2026-01-01T00:00:00Z, monotonic 0.0."""
    return FakeClock()


@pytest.fixture
def cas(tmp_path: Path, fake_clock: FakeClock) -> FsCAS:
    """An empty filesystem CAS for domain "test" under tmp_path (P0-09)."""
    return FsCAS(tmp_path / "cas", "test", clock=fake_clock)


# Hermetic git: no user/system config (hooks, signing, filters), fixed identity.
GIT_TEST_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh git work tree with one commit of `a.sv` and `sub/b.sv` (P0-06)."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "a.sv").write_text("module a; endmodule\n")
    (repo / "sub" / "b.sv").write_text("module b; endmodule\n")
    env = {**os.environ, **GIT_TEST_ENV}
    for argv in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(argv, cwd=repo, env=env, check=True, capture_output=True)
    return repo
