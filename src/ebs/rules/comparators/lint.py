"""`lint` comparator: the violation sets of both runs, after normalization (P3-05 R2).

Every file among an action's outputs (directories are walked) is read as a lint report:

- `*.json`: a list of violations, or an object with a `violations` list; each one has `rule`
  (or `id`), `file`, `line` and `message`;
- anything else: one violation per line matching `pattern` (named groups `rule`, `file`,
  `line`, `message`; default `RULE FILE:LINE[:COL][:] MESSAGE`); other lines are ignored.

A violation is normalized to (rule id, file, line, message template): the file loses `./` and
the runner's work-dir prefix (`…/<build>/<16 hex action dir>/work/`, `ebs.runner.stage`), the
message has quoted text replaced by `<*>`, numbers by `<N>`, and runs of white space collapsed.
The two runs are equivalent when their multisets of normalized violations are equal; otherwise
the verdict lists what was added (`+`) and removed (`-`).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.rules.comparators.api import Summary, Verdict

__all__ = ["DEFAULT_PATTERN", "LintComparator", "Violation", "normalize", "parse_report"]

DEFAULT_PATTERN: Final = (
    r"^\s*(?P<rule>[A-Za-z][\w.-]*)\s+(?P<file>[^\s:]+):(?P<line>\d+)(?::\d+)?:?\s+"
    r"(?P<message>.*\S)\s*$"
)
DEFAULT_MAX_DETAILS: Final = 50
_GROUPS: Final = ("rule", "file", "line", "message")
_SETTINGS: Final = frozenset({"pattern", "max_details"})
_SCRATCH = re.compile(r"^/(?:.*/)?[0-9a-f]{16}/work/")  # <scratch>/<build>/<action_dir_name>/work/
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`']*'|`[^`]*`")
_NUMBER = re.compile(r"\b(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?)\b")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True, order=True)
class Violation:
    rule: str
    file: str
    line: int
    message: str  # the template

    def __str__(self) -> str:
        return f"{self.rule} {self.file}:{self.line}: {self.message}"


def _file(path: str) -> str:
    path = _SCRATCH.sub("", path.replace("\\", "/"))
    while path.startswith("./"):
        path = path[2:]
    return path


def _template(message: str) -> str:
    message = _QUOTED.sub("<*>", message)
    message = _NUMBER.sub("<N>", message)
    return _SPACE.sub(" ", message).strip()


def normalize(rule: object, file: object, line: object, message: object) -> Violation | None:
    """A normalized violation, or None when a field is missing or malformed."""
    if isinstance(line, bool):
        return None
    if isinstance(line, str) and line.strip().isdigit():
        line = int(line)
    if not (isinstance(rule, str) and rule and isinstance(file, str) and file):
        return None
    if not isinstance(line, int) or not isinstance(message, str):
        return None
    return Violation(rule.strip(), _file(file.strip()), line, _template(message))


def _json_violations(path: Path) -> list[Violation]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except ValueError:
        return []
    rows = doc.get("violations") if isinstance(doc, dict) else doc
    found = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            v = normalize(
                row.get("rule", row.get("id")), row.get("file"), row.get("line"),
                row.get("message"),
            )  # fmt: skip
            if v is not None:
                found.append(v)
    return found


def parse_report(path: Path, pattern: re.Pattern[str] | None = None) -> list[Violation]:
    """The normalized violations of one report file, in file order."""
    if path.suffix == ".json":
        return _json_violations(path)
    regex = pattern or re.compile(DEFAULT_PATTERN)
    found = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for text in f:
            m = regex.match(text.rstrip("\n"))
            if m is not None:
                v = normalize(m["rule"], m["file"], m["line"], m["message"])
                if v is not None:
                    found.append(v)
    return found


def _files(outputs: Mapping[str, Path]) -> Iterator[Path]:
    for _, path in sorted(outputs.items()):
        if path.is_dir():
            for root, dirs, names in os.walk(path):
                dirs.sort()
                for name in sorted(names):
                    yield Path(root) / name
        elif path.is_file():
            yield path


class LintComparator:
    name = "lint"

    def __init__(
        self, pattern: str = DEFAULT_PATTERN, max_details: int = DEFAULT_MAX_DETAILS
    ) -> None:
        try:
            self._pattern = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid lint pattern {pattern!r}: {exc}") from None
        missing = [g for g in _GROUPS if g not in self._pattern.groupindex]
        if missing:
            raise ValueError(
                f"the lint pattern needs the named groups rule, file, line and message; "
                f"missing: {', '.join(missing)}"
            )
        if max_details < 1:
            raise ValueError(f"max_details must be at least 1, got {max_details}")
        self.max_details = max_details

    @classmethod
    def from_settings(cls, settings: Mapping[str, object]) -> LintComparator:
        unknown = sorted(set(settings) - _SETTINGS)
        if unknown:
            raise ValueError(f"unknown lint comparator setting(s): {', '.join(unknown)}")
        pattern = settings.get("pattern", DEFAULT_PATTERN)
        limit = settings.get("max_details", DEFAULT_MAX_DETAILS)
        if not isinstance(pattern, str):
            raise ValueError(f"pattern must be a string, got {pattern!r}")
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError(f"max_details must be an integer, got {limit!r}")
        return cls(pattern=pattern, max_details=limit)

    def _violations(self, outputs: Mapping[str, Path]) -> Counter[Violation] | None:
        files = list(_files(outputs))
        if not files:
            return None
        found: Counter[Violation] = Counter()
        for path in files:
            found.update(parse_report(path, self._pattern))
        return found

    def compare(
        self,
        old_outputs: Mapping[str, Path],
        new_outputs: Mapping[str, Path],
        old_summary: Summary,
        new_summary: Summary,
    ) -> Verdict:
        old = self._violations(old_outputs)
        new = self._violations(new_outputs)
        for run, found in (("old run", old), ("new run", new)):
            if found is None:
                return Verdict.incomparable(f"the {run} has no lint report among its outputs")
        assert old is not None
        assert new is not None
        added, removed = new - old, old - new
        if not added and not removed:
            return Verdict.equivalent()
        lines = [f"+ {v}" for v in sorted(added.elements())]
        lines += [f"- {v}" for v in sorted(removed.elements())]
        head = f"{added.total()} added, {removed.total()} removed"
        shown = lines[: self.max_details]
        if len(lines) > len(shown):
            shown.append(f"… and {len(lines) - len(shown)} more")
        return Verdict.changed(head, *shown)
