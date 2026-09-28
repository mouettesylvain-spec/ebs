from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "coverage_gate.py"


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("coverage_gate", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = module
    spec.loader.exec_module(module)
    return module


def _file(stmts: int, covered: int, branches: int = 0, covered_branches: int = 0) -> Any:
    return {
        "summary": {
            "num_statements": stmts,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


def _write(tmp_path: Path, files: dict[str, Any]) -> Path:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps({"meta": {"branch_coverage": True}, "files": files}))
    return path


# R7
def test_package_under_line_threshold_fails(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write(
        tmp_path,
        {
            "src/ebs/core/a.py": _file(100, 94, 10, 10),  # 94 % < 95 %
            "src/ebs/cli/main.py": _file(10, 10),
        },
    )
    assert gate.main([str(report), str(tmp_path / "src")]) != 0
    out = capsys.readouterr().out
    assert "FAIL  ebs/core: line 94.0 %" in out


# R7
def test_package_under_branch_threshold_fails(gate: ModuleType, tmp_path: Path) -> None:
    report = _write(tmp_path, {"src/ebs/plan/keys.py": _file(100, 100, 10, 8)})  # 80 % < 90 %
    assert gate.main([str(report), str(tmp_path / "src")]) != 0


# R7
def test_all_packages_at_threshold_pass(gate: ModuleType, tmp_path: Path) -> None:
    report = _write(
        tmp_path,
        {
            "src/ebs/core/a.py": _file(50, 48, 10, 9),
            "src/ebs/core/b.py": _file(50, 47, 10, 9),  # core: 95 % line, 90 % branch
            "src/ebs/cas/fs.py": _file(20, 20, 4, 4),
            "src/ebs/cli/main.py": _file(10, 5),  # cli has no per-package gate
        },
    )
    assert gate.main([str(report), str(tmp_path / "src")]) == 0


# R7
def test_aggregates_over_files_not_average_of_percentages(gate: ModuleType, tmp_path: Path) -> None:
    # 1/1 and 90/100 average to 95 %, but the package really has 91/101 = 90.1 %.
    report = _write(
        tmp_path,
        {"src/ebs/gc/a.py": _file(1, 1), "src/ebs/gc/b.py": _file(100, 90)},
    )
    assert gate.main([str(report), str(tmp_path / "src")]) != 0


# R7
def test_absent_packages_are_skipped(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write(tmp_path, {"src/ebs/core/a.py": _file(10, 10)})
    assert gate.main([str(report), str(tmp_path / "src")]) == 0
    assert "ebs/sources" in capsys.readouterr().out


# R7
def test_installed_paths_are_recognised(gate: ModuleType, tmp_path: Path) -> None:
    report = _write(tmp_path, {".venv/lib/python3.12/site-packages/ebs/sources/x.py": _file(10, 1)})
    assert gate.main([str(report), str(tmp_path / "src")]) != 0


# R7
def test_package_prefix_does_not_match_similar_names(gate: ModuleType, tmp_path: Path) -> None:
    # ebs/core_extra is not ebs/core.
    report = _write(tmp_path, {"src/ebs/core_extra/x.py": _file(10, 1)})
    assert gate.main([str(report), str(tmp_path / "src")]) == 0


# R7
def test_missing_report_is_an_error(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main([str(tmp_path / "nope.json"), str(tmp_path)]) == 2
    assert "coverage json" in capsys.readouterr().err.lower()


# R7
def test_failure_not_masked_by_later_passing_package(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _write(
        tmp_path, {"src/ebs/core/a.py": _file(100, 90), "src/ebs/gc/a.py": _file(10, 10)}
    )
    assert gate.main([str(report), str(tmp_path / "src")]) == 1
    out = capsys.readouterr().out
    assert "ok    ebs/gc" in out
    assert "1 package(s) below threshold" in out


# R7
def test_existing_package_missing_from_report_fails(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "src" / "ebs" / "core").mkdir(parents=True)
    report = _write(tmp_path, {})  # e.g. coverage `source` pointed at the wrong place
    assert gate.main([str(report), str(tmp_path / "src")]) != 0
    assert "FAIL  ebs/core: exists under" in capsys.readouterr().out


# R7
def test_package_without_statements_passes(gate: ModuleType, tmp_path: Path) -> None:
    report = _write(tmp_path, {"src/ebs/gc/__init__.py": _file(0, 0)})
    assert gate.main([str(report), str(tmp_path / "src")]) == 0
