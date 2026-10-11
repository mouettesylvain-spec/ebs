"""Questa transcript parsing: UVM report counts, `** Error` / `** Fatal` lines, the `$finish`
marker, and the first error line with a normalized signature for clustering failures.

Lines may carry the `# ` prefix vsim puts in its transcript; compiler output has none. Parsing
is streaming (one pass, constant memory), since a regression transcript can be large.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "MAX_LINE",
    "TranscriptSummary",
    "error_signature",
    "parse_transcript",
    "sim_passed",
]

MAX_LINE: Final = 512
"""First error lines and signatures are cut to this many characters."""

_PREFIX = re.compile(r"^#\s?")
_UVM_SUMMARY = re.compile(r"^UVM_(ERROR|FATAL)\s*:\s*(\d+)\s*$")
_UVM_REPORT = re.compile(r"^UVM_(ERROR|FATAL)\s+[^\s:]")
_ERROR = re.compile(r"^\*\* Error\b")
_FATAL = re.compile(r"^\*\* Fatal\b")
_FINISH = re.compile(r"^\*\* Note: \$finish\b")
_CRASH = re.compile(r"^\*\* Fatal: \(SIG(?:SEGV|BUS|ILL|FPE|ABRT)\)")

# Signature normalization, applied in this order.
_PATH = re.compile(r"[\w.$~+-]*(?:/[\w.$~+-]*)+")
_HEX = re.compile(
    r"\b0[xX][0-9a-fA-F_]+\b"
    r"|(?<![\w'])\d*'[sS]?"
    r"(?:[hH][0-9a-fA-F_xXzZ?]+|[bB][01_xXzZ?]+|[oO][0-7_xXzZ?]+|[dD][0-9_xXzZ?]+)(?!\w)"
)
_MESSAGE_ID = re.compile(r"(\([a-z]+-\d+\))")  # (vlog-1001): kept, it names the diagnostic
_NUMBER = re.compile(r"(?<![\w<])\d+(?:\.\d+)?")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class TranscriptSummary:
    """What a transcript says about a run.

    `uvm_errors` / `uvm_fatals` come from the last UVM report summary, or from counting report
    lines when there is none. `errors` / `fatals` count `** Error` / `** Fatal` lines. `finished`
    means a `$finish` marker was seen; `crashed` that the tool reported its own crash signal.
    """

    uvm_errors: int = 0
    uvm_fatals: int = 0
    errors: int = 0
    fatals: int = 0
    finished: bool = False
    crashed: bool = False
    first_error: str | None = None

    @property
    def signature(self) -> str | None:
        return None if self.first_error is None else error_signature(self.first_error)


def _strip(line: str) -> str:
    return _PREFIX.sub("", line.rstrip("\r\n"), count=1).strip()


def parse_transcript(lines: Iterable[str]) -> TranscriptSummary:
    """Summarize a transcript given as lines (a text file object works)."""
    summary: dict[str, int] = {}
    reports = {"ERROR": 0, "FATAL": 0}
    errors = fatals = 0
    finished = crashed = False
    first: str | None = None
    for raw in lines:
        line = _strip(raw)
        if not line:
            continue
        m = _UVM_SUMMARY.match(line)
        if m is not None:
            summary[m[1]] = int(m[2])
            continue
        m = _UVM_REPORT.match(line)
        if m is not None:
            reports[m[1]] += 1
        elif _ERROR.match(line):
            errors += 1
        elif _FATAL.match(line):
            fatals += 1
            crashed = crashed or _CRASH.match(line) is not None
        else:
            finished = finished or _FINISH.match(line) is not None
            continue
        if first is None:
            first = line[:MAX_LINE]
    return TranscriptSummary(
        uvm_errors=summary.get("ERROR", reports["ERROR"]),
        uvm_fatals=summary.get("FATAL", reports["FATAL"]),
        errors=errors,
        fatals=fatals,
        finished=finished,
        crashed=crashed,
        first_error=first,
    )


def error_signature(line: str) -> str:
    """`line` with paths, hex/based literals and numbers replaced by `<path>`, `<hex>`, `<n>`,
    whitespace collapsed: failures that differ only in those cluster together. Message ids such
    as `(vlog-1001)` are kept."""
    # Message ids like `(vlog-1001)` name the diagnostic: they are kept, the rest normalized.
    parts = _MESSAGE_ID.split(_strip(line))
    for i in range(0, len(parts), 2):
        text = _PATH.sub(_path, parts[i])
        text = _HEX.sub("<hex>", text)
        parts[i] = _NUMBER.sub("<n>", text)
    return _SPACE.sub(" ", "".join(parts)).strip()[:MAX_LINE]


def _path(m: re.Match[str]) -> str:
    return m[0] if m[0].strip("/") == "" else "<path>"  # a lone `/` (a ratio) is not a path


def sim_passed(exit_code: int, summary: TranscriptSummary) -> bool:
    """PASSED iff exit 0, no fatals, no UVM errors and the run reached `$finish`."""
    return (
        exit_code == 0
        and summary.uvm_errors == 0
        and summary.uvm_fatals == 0
        and summary.fatals == 0
        and summary.finished
    )
