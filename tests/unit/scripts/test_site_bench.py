"""Tests for scripts/site_bench.py: the landing page's measured numbers (task D-01 R8).

The builds themselves need PostgreSQL and run in the site workflow; here the parsing, the edits
each scenario makes to the examples, and the document shape are checked.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "site_bench.py"


@pytest.fixture(scope="module")
def bench() -> ModuleType:
    spec = importlib.util.spec_from_file_location("site_bench", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_bench"] = module
    spec.loader.exec_module(module)
    return module


def _build_output(*events: tuple[str, str], status: str = "passed") -> str:
    lines = [json.dumps({"type": t, "action_id": a}) for t, a in events]
    lines.append(json.dumps({"v": 1, "status": status}))
    return "\n".join(lines) + "\n"


# R8
def test_parse_build_counts(bench: ModuleType) -> None:
    out = _build_output(
        ("cache_hit", "lint[block=alu]"),
        ("submitted", "lint[block=counter]"),
        ("finished", "lint[block=counter]"),
        ("submitted", "summary"),
    )
    counts = bench.parse_build(out)
    assert (counts.executed, counts.cached, counts.total) == (2, 1, 3)


def test_parse_build_rejects_failed_build(bench: ModuleType) -> None:
    with pytest.raises(ValueError, match="failed"):
        bench.parse_build(_build_output(("submitted", "a"), status="failed"))


# R8: the edits are written against the example sources; a drifted example breaks them here.
@pytest.mark.parametrize("index", range(5))
def test_scenario_edits_apply(bench: ModuleType, index: int, tmp_path: Path) -> None:
    scenario = bench.SCENARIOS[index]
    copy = tmp_path / scenario.example
    shutil.copytree(ROOT / "examples" / scenario.example, copy)
    before = {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    if scenario.edit is None:
        return
    scenario.edit(copy)
    after = {p: p.read_bytes() for p in copy.rglob("*") if p.is_file()}
    assert before != after, scenario.id


def test_scenarios_cover_the_story(bench: ModuleType) -> None:
    ids = [s.id for s in bench.SCENARIOS]
    assert ids == ["lint-cold", "lint-warm", "lint-comment", "sim-cold", "sim-seed"]


# R8
def test_document_shape(bench: ModuleType) -> None:
    counts = bench.BuildCounts(executed=1, cached=2, total=3)
    result = bench.ScenarioResult(bench.SCENARIOS[2], counts, wall_s=1.234)
    doc = bench.bench_document(
        "abc1234", "2026-10-17T12:00:00Z", [result], make_executed=2, make_total=2
    )
    assert doc["v"] == 1
    assert doc["commit"] == "abc1234"
    assert doc["make"] == {"example": "lint-make", "executed": 2, "total": 2}
    (row,) = doc["scenarios"]
    assert row == {
        "id": "lint-comment",
        "example": "lint-make",
        "label": bench.SCENARIOS[2].label,
        "executed": 1,
        "cached": 2,
        "total": 3,
        "wall_s": 1.23,
    }


# R8: Make has no cache to reuse; a fresh copy of the lint Makefile is stale everywhere.
def test_make_rebuilds_every_lint_target(bench: ModuleType, tmp_path: Path) -> None:
    if shutil.which("make") is None:
        pytest.skip("missing dependency: make")
    assert bench.make_rebuilds(tmp_path) == len(bench.lint_blocks()) == 2


def test_committed_document_matches_shape(bench: ModuleType) -> None:
    doc = json.loads((ROOT / "site" / "src" / "data" / "bench.json").read_text(encoding="utf-8"))
    assert doc["v"] == 1
    assert [s["id"] for s in doc["scenarios"]] == [s.id for s in bench.SCENARIOS]
    for row in doc["scenarios"]:
        assert row["executed"] + row["cached"] == row["total"]
