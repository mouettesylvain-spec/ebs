"""`sim` comparator: one simulation (a test with one seed) of both runs (P3-05 R2).

The outcome is read from the `result` output (the `questa.sim` result document, schema 1, final)
or, when that is missing or not final, from the summary. The runs must be the same (test, seed);
then their pass/fail status must be equal, and their coverage (`coverage`, a percentage, when
either run reports one) must differ by at most `coverage_tolerance` points.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ebs.rules.comparators.api import Summary, Verdict

__all__ = ["DEFAULT_COVERAGE_TOLERANCE", "SimComparator"]

DEFAULT_COVERAGE_TOLERANCE: Final = 0.5
_RESULT_SCHEMA: Final = 1
_FIELDS: Final = ("status", "test", "seed", "coverage")
_SETTINGS: Final = frozenset({"coverage_tolerance"})


def _result_doc(path: Path | None) -> dict[str, object] | None:
    if path is None or not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != _RESULT_SCHEMA:
        return None
    return doc if doc.get("status") in ("passed", "failed") else None


def _outcome(outputs: Mapping[str, Path], summary: Summary) -> dict[str, object] | None:
    doc = _result_doc(outputs.get("result"))
    if doc is not None:
        return {k: doc[k] for k in _FIELDS if k in doc}
    if summary.get("status") in ("passed", "failed"):
        return {k: summary[k] for k in _FIELDS if k in summary}
    return None


def _coverage(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().removesuffix("%"))
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


class SimComparator:
    name = "sim"

    def __init__(self, coverage_tolerance: float = DEFAULT_COVERAGE_TOLERANCE) -> None:
        if not (math.isfinite(coverage_tolerance) and coverage_tolerance >= 0):
            raise ValueError(
                f"coverage_tolerance must be a non-negative number of percentage points, got "
                f"{coverage_tolerance!r}"
            )
        self.coverage_tolerance = float(coverage_tolerance)

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> SimComparator:
        unknown = sorted(set(settings) - _SETTINGS)
        if unknown:
            raise ValueError(f"unknown sim comparator setting(s): {', '.join(unknown)}")
        tolerance = settings.get("coverage_tolerance", DEFAULT_COVERAGE_TOLERANCE)
        if isinstance(tolerance, bool) or not isinstance(tolerance, int | float):
            raise ValueError(f"coverage_tolerance must be a number, got {tolerance!r}")
        return cls(coverage_tolerance=float(tolerance))

    def compare(
        self,
        old_outputs: Mapping[str, Path],
        new_outputs: Mapping[str, Path],
        old_summary: Summary,
        new_summary: Summary,
    ) -> Verdict:
        old = _outcome(old_outputs, old_summary)
        new = _outcome(new_outputs, new_summary)
        for run, outcome in (("old run", old), ("new run", new)):
            if outcome is None:
                return Verdict.incomparable(f"the {run} has no simulation result")
        assert old is not None
        assert new is not None
        for field in ("test", "seed"):
            if old.get(field) != new.get(field):
                return Verdict.incomparable(
                    f"{field} differs: the old run is {old.get(field)!r}, the new run "
                    f"{new.get(field)!r}; the runs are not the same simulation"
                )
        details: list[str] = []
        if old["status"] != new["status"]:
            details.append(f"status: {old['status']} -> {new['status']}")
        if "coverage" in old or "coverage" in new:
            if "coverage" not in new or "coverage" not in old:
                which = "old" if "coverage" in old else "new"
                details.append(f"coverage: reported only by the {which} run")
            else:
                before, after = _coverage(old["coverage"]), _coverage(new["coverage"])
                if before is None or after is None:
                    return Verdict.incomparable(
                        f"coverage is not a number: {old['coverage']!r} -> {new['coverage']!r}"
                    )
                delta = after - before
                if abs(delta) > self.coverage_tolerance + 1e-9:
                    details.append(
                        f"coverage: {before:.2f} -> {after:.2f} ({delta:+.2f}, tolerance "
                        f"{self.coverage_tolerance:.2f})"
                    )
        return Verdict.changed(*details) if details else Verdict.equivalent()
