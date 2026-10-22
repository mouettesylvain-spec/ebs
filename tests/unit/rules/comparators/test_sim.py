"""The `questa.sim` comparator (P3-05 R2): per (test, seed) pass/fail equality and the coverage
delta within a tolerance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs.rules.comparators.api import Verdict, comparator_names, get_comparator
from ebs.rules.comparators.sim import SimComparator

Summary = dict[str, str | int]


def _result(tmp: Path, name: str, **fields: object) -> dict[str, Path]:
    path = tmp / name / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": 1, **fields}))
    return {"result": path}


def _summary(**fields: str | int) -> Summary:
    return dict(fields)


CASES: list[tuple[str, Summary, Summary, str, tuple[str, ...]]] = [
    # (case, old summary, new summary, verdict kind, expected detail fragments)
    ("same pass", _summary(status="passed", test="t", seed=1),
     _summary(status="passed", test="t", seed=1), "equivalent", ()),
    ("same fail", _summary(status="failed", test="t", seed=1, signature="x"),
     _summary(status="failed", test="t", seed=1, signature="x"), "equivalent", ()),
    ("pass to fail", _summary(status="passed", test="t", seed=1),
     _summary(status="failed", test="t", seed=1), "changed", ("status: passed -> failed",)),
    ("fail to pass", _summary(status="failed", test="t", seed=7),
     _summary(status="passed", test="t", seed=7), "changed", ("status: failed -> passed",)),
    ("other seed", _summary(status="passed", test="t", seed=1),
     _summary(status="passed", test="t", seed=2), "incomparable", ("seed",)),
    ("other test", _summary(status="passed", test="a", seed=1),
     _summary(status="passed", test="b", seed=1), "incomparable", ("test",)),
    ("no result", {}, _summary(status="passed", test="t", seed=1), "incomparable",
     ("old run",)),
    ("coverage within", _summary(status="passed", test="t", seed=1, coverage="80.0"),
     _summary(status="passed", test="t", seed=1, coverage="80.4"), "equivalent", ()),
    ("coverage at tolerance", _summary(status="passed", test="t", seed=1, coverage="80.0"),
     _summary(status="passed", test="t", seed=1, coverage="80.5"), "equivalent", ()),
    ("coverage drop", _summary(status="passed", test="t", seed=1, coverage="80.0"),
     _summary(status="passed", test="t", seed=1, coverage="79.0"), "changed",
     ("coverage: 80.00 -> 79.00 (-1.00, tolerance 0.50)",)),
    ("coverage rise", _summary(status="passed", test="t", seed=1, coverage=80),
     _summary(status="passed", test="t", seed=1, coverage=82), "changed", ("+2.00",)),
    ("coverage one side", _summary(status="passed", test="t", seed=1, coverage="80"),
     _summary(status="passed", test="t", seed=1), "changed", ("reported only by the old run",)),
    ("coverage not a number", _summary(status="passed", test="t", seed=1, coverage="n/a"),
     _summary(status="passed", test="t", seed=1, coverage="80"), "incomparable",
     ("coverage",)),
    ("status and coverage", _summary(status="passed", test="t", seed=1, coverage="80"),
     _summary(status="failed", test="t", seed=1, coverage="70"), "changed",
     ("status: passed -> failed", "coverage: 80.00 -> 70.00")),
    ("coverage nan", _summary(status="passed", test="t", seed=1, coverage="nan"),
     _summary(status="passed", test="t", seed=1, coverage="80"), "incomparable", ("coverage",)),
    ("coverage percent sign", _summary(status="passed", test="t", seed=1, coverage="80%"),
     _summary(status="passed", test="t", seed=1, coverage="80"), "equivalent", ()),
    ("coverage inside tolerance", _summary(status="passed", test="t", seed=1, coverage="80.1"),
     _summary(status="passed", test="t", seed=1, coverage="80.6"), "equivalent", ()),
    ("not a result status", _summary(status="error", test="t", seed=1),
     _summary(status="error", test="t", seed=1), "incomparable", ("old run",)),
]  # fmt: skip


# R2
@pytest.mark.parametrize(("old", "new", "kind", "details"),
                         [c[1:] for c in CASES], ids=[c[0] for c in CASES])  # fmt: skip
def test_summaries(old: Summary, new: Summary, kind: str, details: tuple[str, ...]) -> None:
    verdict = SimComparator(coverage_tolerance=0.5).compare({}, {}, old, new)
    assert verdict.kind == kind, verdict
    text = "\n".join(verdict.details)
    for fragment in details:
        assert fragment in text
    if kind == "equivalent":
        assert verdict.details == ()


# R2: the result document is read in preference to the summary (it is the output itself)
def test_result_document_wins(tmp_path: Path) -> None:
    old = _result(tmp_path, "old", status="passed", test="t", seed=3)
    new = _result(tmp_path, "new", status="failed", test="t", seed=3)
    same = _summary(status="passed", test="t", seed=3)
    verdict = SimComparator().compare(old, new, same, same)
    assert verdict == Verdict.changed("status: passed -> failed")


# R2: a result document that is not final falls back to the summary
def test_unreadable_result_uses_summary(tmp_path: Path) -> None:
    old = _result(tmp_path, "old", ebs_pending="questa.sim")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    same = _summary(status="passed", test="t", seed=3)
    assert SimComparator().compare(old, {"result": bad}, same, same) == Verdict.equivalent()


# R2: a tighter tolerance turns the same delta into a change
def test_tolerance_is_configurable() -> None:
    old = _summary(status="passed", test="t", seed=1, coverage="80.0")
    new = _summary(status="passed", test="t", seed=1, coverage="80.4")
    assert SimComparator(coverage_tolerance=0.5).compare({}, {}, old, new).kind == "equivalent"
    assert SimComparator(coverage_tolerance=0.1).compare({}, {}, old, new).kind == "changed"
    with pytest.raises(ValueError, match="tolerance"):
        SimComparator(coverage_tolerance=-1)


# R2: comparators are looked up by name
def test_registered() -> None:
    assert {"sim", "lint"} <= set(comparator_names())
    assert isinstance(get_comparator("sim", {"coverage_tolerance": 0.25}), SimComparator)


# R2: a result document of another schema is not trusted; the summary decides
def test_other_schema_uses_summary(tmp_path: Path) -> None:
    old = _result(tmp_path, "old", status="failed", test="t", seed=3)
    old["result"].write_text(json.dumps({"schema": 2, "status": "failed", "test": "t", "seed": 3}))
    same = _summary(status="passed", test="t", seed=3)
    assert SimComparator().compare(old, {}, same, same) == Verdict.equivalent()


# R2: settings are checked
def test_bad_settings() -> None:
    with pytest.raises(ValueError, match="tolerance"):
        SimComparator(coverage_tolerance=float("inf"))
    with pytest.raises(ValueError, match="unknown"):
        SimComparator.from_settings({"tolerance": 1})
    with pytest.raises(ValueError, match="number"):
        SimComparator.from_settings({"coverage_tolerance": True})
    assert SimComparator.from_settings({"coverage_tolerance": 0}).coverage_tolerance == 0.0
