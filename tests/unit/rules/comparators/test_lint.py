"""The lint comparator (P3-05 R2): the violation set diff after normalization (rule id + file +
line + message template)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebs.rules.comparators.api import Verdict
from ebs.rules.comparators.lint import LintComparator, Violation, normalize, parse_report

BASE = """\
W123 rtl/alu.sv:10: signal 'carry' is never read
E200 rtl/alu.sv:42: width mismatch: 8 bits vs 16 bits
"""


def _report(tmp: Path, name: str, text: str, file: str = "lint.rpt") -> dict[str, Path]:
    path = tmp / name / file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {"reports": path}


CASES: list[tuple[str, str, str, str, tuple[str, ...]]] = [
    # (case, old report, new report, verdict kind, expected detail lines)
    ("identical", BASE, BASE, "equivalent", ()),
    ("reordered", BASE, "\n".join(reversed(BASE.splitlines())) + "\n", "equivalent", ()),
    ("quoted names differ", BASE,
     BASE.replace("'carry'", "'cout'"), "equivalent", ()),
    ("numbers differ", BASE, BASE.replace("8 bits vs 16", "4 bits vs 32"), "equivalent", ()),
    ("scratch path", BASE,
     BASE.replace("rtl/alu.sv:10", "/tmp/ebs/b1/0123456789abcdef/work/rtl/alu.sv:10"),
     "equivalent", ()),
    ("leading dot", BASE, BASE.replace("rtl/alu.sv:42", "./rtl/alu.sv:42"), "equivalent", ()),
    ("whitespace", BASE, BASE.replace("never read", "never   read"), "equivalent", ()),
    ("new violation", BASE, BASE + "W9 rtl/fpu.sv:3: unused port 'x'\n", "changed",
     ("+ W9 rtl/fpu.sv:3: unused port <*>",)),
    ("gone violation", BASE, BASE.splitlines()[1] + "\n", "changed",
     ("- W123 rtl/alu.sv:10: signal <*> is never read",)),
    ("other line", BASE, BASE.replace("alu.sv:42", "alu.sv:43"), "changed",
     ("- E200 rtl/alu.sv:42", "+ E200 rtl/alu.sv:43")),
    ("other rule", BASE, BASE.replace("W123", "W124"), "changed", ("+ W124", "- W123")),
    ("other message", BASE, BASE.replace("never read", "never written"), "changed",
     ("+ W123 rtl/alu.sv:10: signal <*> is never written",)),
    ("duplicate added", BASE, BASE + BASE.splitlines()[0] + "\n", "changed", ("+ W123",)),
    ("noise lines ignored", BASE, "lint started\n" + BASE + "2 violations\n", "equivalent", ()),
    ("both clean", "", "", "equivalent", ()),
]  # fmt: skip


# R2
@pytest.mark.parametrize(("old", "new", "kind", "details"),
                         [c[1:] for c in CASES], ids=[c[0] for c in CASES])  # fmt: skip
def test_text_reports(
    tmp_path: Path, old: str, new: str, kind: str, details: tuple[str, ...]
) -> None:
    verdict = LintComparator().compare(
        _report(tmp_path, "old", old), _report(tmp_path, "new", new), {}, {}
    )
    assert verdict.kind == kind, verdict
    text = "\n".join(verdict.details)
    for fragment in details:
        assert fragment in text
    if kind == "equivalent":
        assert verdict.details == ()


# R2: JSON reports (a list, or an object with `violations`), and a report directory
def test_json_reports_and_directories(tmp_path: Path) -> None:
    rows = [
        {"rule": "W123", "file": "rtl/alu.sv", "line": 10, "message": "signal 'carry' unused"},
        {"id": "E200", "file": "./rtl/alu.sv", "line": "42", "message": "width 8 vs 16"},
    ]
    old = _report(tmp_path, "old", json.dumps({"violations": rows}), "lint.json")
    new_dir = tmp_path / "new" / "reports"
    new_dir.mkdir(parents=True)
    (new_dir / "a.json").write_text(json.dumps(rows[:1]))
    (new_dir / "sub").mkdir()
    (new_dir / "sub" / "b.rpt").write_text("E200 rtl/alu.sv:42: width 4 vs 32\n")
    assert LintComparator().compare(old, {"reports": new_dir}, {}, {}) == Verdict.equivalent()
    assert parse_report(old["reports"]) == [
        Violation("W123", "rtl/alu.sv", 10, "signal <*> unused"),
        Violation("E200", "rtl/alu.sv", 42, "width <N> vs <N>"),
    ]


# R2: with no report on one side there is nothing to diff
def test_missing_report_is_incomparable(tmp_path: Path) -> None:
    old = _report(tmp_path, "old", BASE)
    verdict = LintComparator().compare(old, {}, {}, {})
    assert verdict.kind == "incomparable"
    assert "new run" in verdict.details[0]


# R2: a long diff is capped, with the count of what is not shown
def test_long_diff_is_capped(tmp_path: Path) -> None:
    many = "".join(f"W1 rtl/a.sv:{n}: unused\n" for n in range(1, 60))
    verdict = LintComparator(max_details=10).compare(
        _report(tmp_path, "old", ""), _report(tmp_path, "new", many), {}, {}
    )
    assert verdict.kind == "changed"
    assert len(verdict.details) == 12  # head, 10 shown, the rest
    assert verdict.details[0] == "59 added, 0 removed"
    assert verdict.details[-1] == "… and 49 more"


# R2: the line format is configurable (a site's lint tool)
def test_custom_line_pattern(tmp_path: Path) -> None:
    pattern = r"^\[(?P<rule>\w+)\] (?P<message>.+) at (?P<file>\S+) line (?P<line>\d+)$"
    old = _report(tmp_path, "old", "[R1] bad 'x' at rtl/a.sv line 4\n")
    new = _report(tmp_path, "new", "[R1] bad 'y' at rtl/a.sv line 4\n")
    assert LintComparator(pattern=pattern).compare(old, new, {}, {}) == Verdict.equivalent()
    with pytest.raises(ValueError, match="rule"):
        LintComparator(pattern=r"(?P<file>\S+)")


# R2: only the runner's work-dir prefix and `./` are stripped from file names
def test_file_normalization() -> None:
    assert normalize("R", "././rtl/a.sv", 1, "m") == Violation("R", "rtl/a.sv", 1, "m")
    work = "/scratch/ebs/b1/0123456789abcdef/work/rtl/a.sv"
    assert normalize("R", work, 1, "m") == Violation("R", "rtl/a.sv", 1, "m")
    for kept in ("/proj/ebs/rtl/core/alu.sv", "myebs/x/rtl/a.sv", "/proj/work/rtl/a.sv"):
        assert normalize("R", kept, 1, "m") == Violation("R", kept, 1, "m")
    assert normalize("R", "a.sv", True, "m") is None


# R2: settings are checked; exactly max_details lines are shown without a "more" line
def test_settings_and_cap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        LintComparator(max_details=0)
    with pytest.raises(ValueError, match="unknown"):
        LintComparator.from_settings({"regex": "x"})
    assert LintComparator.from_settings({"max_details": 3}).max_details == 3
    three = "".join(f"W1 rtl/a.sv:{n}: unused\n" for n in range(1, 4))
    verdict = LintComparator(max_details=3).compare(
        _report(tmp_path, "old", ""), _report(tmp_path, "new", three), {}, {}
    )
    assert len(verdict.details) == 4
    assert not verdict.details[-1].startswith("…")
