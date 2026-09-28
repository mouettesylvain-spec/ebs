from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

CONFTEST = Path(__file__).resolve().parents[1] / "conftest.py"


@pytest.fixture
def suite(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A throw-away test tree that uses the real tests/conftest.py."""
    monkeypatch.delenv("CI", raising=False)
    pytester.makeconftest(CONFTEST.read_text())
    return pytester


def _add(pytester: pytest.Pytester, relpath: str, body: str) -> None:
    path = pytester.path / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


# R3
@pytest.mark.parametrize(
    ("directory", "marker"),
    [
        ("unit", "unit"),
        ("unit/core", "unit"),
        ("integration", "integration"),
        ("e2e", "integration"),
        ("grid", "grid"),
        ("vendor", "vendor"),
        ("golden", "unit"),
    ],
)
def test_auto_marker_by_directory(suite: pytest.Pytester, directory: str, marker: str) -> None:
    _add(suite, f"{directory}/test_sample.py", "def test_x():\n    pass\n")
    others = {"unit", "integration", "grid", "vendor"} - {marker}

    selected = suite.runpytest("--import-mode=importlib", "-m", marker)
    selected.assert_outcomes(passed=1)
    deselected = suite.runpytest("--import-mode=importlib", "-m", " or ".join(sorted(others)))
    deselected.assert_outcomes(deselected=1)


# R3
def test_explicit_marker_is_kept_under_contract(suite: pytest.Pytester) -> None:
    _add(
        suite,
        "contract/test_sample.py",
        "import pytest\n\n@pytest.mark.integration\ndef test_x():\n    pass\n",
    )
    suite.runpytest("--import-mode=importlib", "-m", "integration").assert_outcomes(passed=1)
    suite.runpytest("--import-mode=importlib", "-m", "unit").assert_outcomes(deselected=1)


# R3
def test_strict_markers_rejects_unknown_marker(suite: pytest.Pytester) -> None:
    _add(
        suite,
        "unit/test_sample.py",
        "import pytest\n\n@pytest.mark.unitt\ndef test_x():\n    pass\n",
    )
    result = suite.runpytest("--import-mode=importlib", "--strict-markers")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*'unitt' not found in `markers` configuration option*"])


# R3
def test_known_markers_are_registered(suite: pytest.Pytester) -> None:
    _add(
        suite,
        "unit/test_sample.py",
        "import pytest\n\n@pytest.mark.slow\ndef test_x():\n    pass\n",
    )
    suite.runpytest("--import-mode=importlib", "--strict-markers").assert_outcomes(passed=1)


MISSING_DEP = (
    "import pytest\n\n"
    "def test_needs_postgres():\n"
    "    pytest.skip('missing dependency: postgres (set EBS_TEST_PG_URL)')\n"
)


# R4
def test_ci_strict_missing_dependency_fails(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(suite, "integration/test_pg.py", MISSING_DEP)
    monkeypatch.setenv("CI", "true")
    result = suite.runpytest("--import-mode=importlib")
    result.assert_outcomes(failed=1)
    result.stdout.fnmatch_lines(["*missing dependency: postgres*"])


# R4
def test_missing_dependency_skips_locally(suite: pytest.Pytester) -> None:
    _add(suite, "integration/test_pg.py", MISSING_DEP)
    suite.runpytest("--import-mode=importlib").assert_outcomes(skipped=1)


# R4
def test_ci_strict_importorskip_fails(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(
        suite,
        "e2e/test_mod.py",
        "import pytest\n\npytest.importorskip('ebs_no_such_module_xyz')\n\n"
        "def test_x():\n    pass\n",
    )
    monkeypatch.setenv("CI", "true")
    result = suite.runpytest("--import-mode=importlib")
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*ebs_no_such_module_xyz*"])


# R4
def test_ci_strict_keeps_ordinary_skips(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(
        suite,
        "integration/test_other.py",
        "import pytest\n\ndef test_x():\n    pytest.skip('not relevant on this platform')\n",
    )
    monkeypatch.setenv("CI", "true")
    suite.runpytest("--import-mode=importlib").assert_outcomes(skipped=1)


# R4
def test_unit_missing_dependency_in_ci_is_not_escalated(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(suite, "unit/test_pg.py", MISSING_DEP)
    monkeypatch.setenv("CI", "true")
    suite.runpytest("--import-mode=importlib").assert_outcomes(skipped=1)


# R4
def test_ci_strict_applies_to_integration_marked_contract_tests(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(
        suite,
        "contract/test_store.py",
        "import pytest\n\n@pytest.mark.integration\ndef test_pg():\n"
        "    pytest.skip('missing dependency: postgres')\n",
    )
    monkeypatch.setenv("CI", "true")
    suite.runpytest("--import-mode=importlib").assert_outcomes(failed=1)


# R3
@pytest.mark.parametrize("relpath", ["test_top.py", "fakes/slurm/test_fake.py"])
def test_unlayered_tests_are_rejected(suite: pytest.Pytester, relpath: str) -> None:
    _add(suite, relpath, "def test_x():\n    pass\n")
    result = suite.runpytest("--import-mode=importlib")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*tests without a layer marker*test_x*"])


# R3
def test_explicitly_marked_test_outside_layer_dirs_is_accepted(suite: pytest.Pytester) -> None:
    _add(
        suite,
        "fakes/slurm/test_fake.py",
        "import pytest\n\n@pytest.mark.unit\ndef test_x():\n    pass\n",
    )
    suite.runpytest("--import-mode=importlib", "-m", "unit").assert_outcomes(passed=1)


# R3
def test_unmarked_contract_test_defaults_to_unit(suite: pytest.Pytester) -> None:
    _add(suite, "contract/test_sample.py", "def test_x():\n    pass\n")
    suite.runpytest("--import-mode=importlib", "-m", "unit").assert_outcomes(passed=1)
    suite.runpytest(
        "--import-mode=importlib", "-m", "integration or grid or vendor"
    ).assert_outcomes(deselected=1)


# R4
@pytest.mark.parametrize("ci_value", ["true", "TRUE", "1", "yes"])
def test_ci_strict_recognises_ci_values(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, ci_value: str
) -> None:
    _add(
        suite,
        "integration/test_pg.py",
        "import pytest\n\ndef test_pg():\n    pytest.skip('Missing dependency: postgres')\n",
    )
    monkeypatch.setenv("CI", ci_value)
    suite.runpytest("--import-mode=importlib").assert_outcomes(failed=1)


# R4
def test_ci_strict_escalates_fixture_level_skips(
    suite: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add(
        suite,
        "integration/test_pg.py",
        "import pytest\n\n@pytest.fixture\ndef pg():\n"
        "    pytest.skip('missing dependency: postgres')\n\n"
        "def test_pg(pg):\n    pass\n\n"
        "@pytest.mark.skipif(True, reason='missing dependency: docker')\n"
        "def test_docker():\n    pass\n",
    )
    monkeypatch.setenv("CI", "true")
    result = suite.runpytest("--import-mode=importlib")
    assert result.ret != 0
    result.assert_outcomes(errors=2)
