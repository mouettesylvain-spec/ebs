"""Per-package coverage gate (docs/design/testing.md § Coverage gates).

Usage: python scripts/coverage_gate.py [coverage.json [SRC_ROOT]]

Reads a `coverage json` report and exits non-zero when a gated package is below its line or
branch threshold. The overall 85 % gate is enforced by coverage itself (`fail_under`).
Packages that do not exist under SRC_ROOT (default `src`) yet are skipped; a package that exists
but is absent from the report fails, because that means coverage was misconfigured.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

# package -> (min line %, min branch %)
THRESHOLDS: Mapping[str, tuple[float, float]] = {
    "ebs/core": (95.0, 90.0),
    "ebs/plan": (95.0, 90.0),
    "ebs/cas": (95.0, 90.0),
    "ebs/sources": (95.0, 90.0),
    "ebs/gc": (95.0, 90.0),
}


@dataclass
class Totals:
    statements: int = 0
    covered_lines: int = 0
    branches: int = 0
    covered_branches: int = 0

    @property
    def line_pct(self) -> float:
        return 100.0 * self.covered_lines / self.statements if self.statements else 100.0

    @property
    def branch_pct(self) -> float:
        return 100.0 * self.covered_branches / self.branches if self.branches else 100.0


def _package_of(path: str) -> str | None:
    parts = PurePosixPath(path.replace("\\", "/")).parts
    for i in range(len(parts) - 1):
        candidate = f"{parts[i]}/{parts[i + 1]}"
        if candidate in THRESHOLDS:
            return candidate
    return None


def aggregate(report: Mapping[str, Any]) -> dict[str, Totals]:
    totals: dict[str, Totals] = {}
    for path, data in report.get("files", {}).items():
        package = _package_of(path)
        if package is None:
            continue
        summary = data["summary"]
        t = totals.setdefault(package, Totals())
        t.statements += int(summary.get("num_statements", 0))
        t.covered_lines += int(summary.get("covered_lines", 0))
        t.branches += int(summary.get("num_branches", 0))
        t.covered_branches += int(summary.get("covered_branches", 0))
    return totals


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    report_path = Path(args[0] if args else "coverage.json")
    src_root = Path(args[1] if len(args) > 1 else "src")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"coverage_gate: cannot read coverage JSON {report_path}: {exc}. "
            "Run pytest with --cov-report=json first.\n"
        )
        return 2

    totals = aggregate(report)
    failures = 0
    for package, (min_line, min_branch) in THRESHOLDS.items():
        t = totals.get(package)
        if t is None:
            if (src_root / package).is_dir():
                failures += 1
                sys.stdout.write(
                    f"  FAIL  {package}: exists under {src_root} but is not in the report; "
                    "check [tool.coverage.run] source\n"
                )
            else:
                sys.stdout.write(f"  skip  {package}: not created yet\n")
            continue
        ok = t.line_pct >= min_line and t.branch_pct >= min_branch
        failures += not ok
        sys.stdout.write(
            f"  {'ok  ' if ok else 'FAIL'}  {package}: line {t.line_pct:.1f} % (min {min_line}), "
            f"branch {t.branch_pct:.1f} % (min {min_branch})\n"
        )
    if failures:
        sys.stdout.write(f"coverage_gate: {failures} package(s) below threshold\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
