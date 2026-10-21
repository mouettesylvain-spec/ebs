"""Resource tuning hints from observed usage (P1-05 R4): hints, never enforcement.

After a build, the driver appends the measured usage of every action that ran and passed (its
ResultManifest's `resources`) to `<workdir>/.ebs/usage.jsonl`, one JSON line per action. Cache
hits add nothing (no new run). `ebs plan` reads the most recent samples per step and prints a
hint when a step requests more than `HINT_FACTOR` x the p95 of the peak memory observed for it.

The log is per work directory (what this user ran); it is compacted to the most recent
`max_samples` per step once it grows past `max_lines`. A damaged line is skipped, and an
unreadable log gives no hints. Two builds finishing at once in one work dir may lose a few of
each other's samples during compaction (read, then replace): acceptable for a hint.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ebs.core.log import get_logger
from ebs.flow.model import format_memory
from ebs.plan.types import Plan

__all__ = [
    "HINT_FACTOR",
    "MIN_SAMPLES",
    "MemoryHint",
    "UsageLog",
    "UsageSample",
    "format_mem",
    "memory_hints",
    "p95",
    "record_usage",
    "usage_path",
]

_log = get_logger(__name__)

HINT_FACTOR: Final = 4.0  # hint when requested memory > HINT_FACTOR x p95 observed
MIN_SAMPLES: Final = 5  # fewer successful runs than this say too little
SUGGEST_FACTOR: Final = 1.5  # suggested request: p95 x this, rounded up to a MiB
MAX_SAMPLES: Final = 200  # most recent samples per step that count
MAX_LINES: Final = 20_000  # compaction threshold of the log
_KIB: Final = 1 << 10
_MIB: Final = 1 << 20


def usage_path(workdir: Path) -> Path:
    return workdir / ".ebs" / "usage.jsonl"


@dataclass(frozen=True, slots=True)
class UsageSample:
    """What one successful action used (`ResourceUsage`), by step."""

    step: str
    max_rss_kb: int
    cpu_s: int
    wall_s: int

    def to_json(self) -> dict[str, object]:
        return {
            "step": self.step,
            "max_rss_kb": self.max_rss_kb,
            "cpu_s": self.cpu_s,
            "wall_s": self.wall_s,
        }


class UsageLog:
    """Append-only JSON lines of `UsageSample`s, compacted when it grows past `max_lines`."""

    def __init__(
        self, path: Path, *, max_samples: int = MAX_SAMPLES, max_lines: int = MAX_LINES
    ) -> None:
        self.path = path
        self._max_samples = max_samples
        self._max_lines = max_lines

    def record(self, samples: Iterable[UsageSample]) -> None:
        lines = [json.dumps(s.to_json(), sort_keys=True) + "\n" for s in samples]
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as f:
            if f.tell() > 0:  # after a crash mid-line, start a fresh line
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    lines.insert(0, "\n")
            f.write("".join(lines).encode())
        self._compact()

    def samples(self) -> list[UsageSample]:
        """Every readable sample, oldest first."""
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return []
        except OSError as exc:  # hints are advisory: never fail `ebs plan` for them
            _log.warning("driver.usage_unreadable", path=str(self.path), error=str(exc))
            return []
        found: list[UsageSample] = []
        for line in text.splitlines():
            sample = _parse(line)
            if sample is not None:
                found.append(sample)
        return found

    def by_step(self) -> dict[str, list[int]]:
        """Peak memory (KiB) of the most recent `max_samples` runs per step, oldest first."""
        out: dict[str, list[int]] = {}
        for sample in self.samples():
            out.setdefault(sample.step, []).append(sample.max_rss_kb)
        return {step: values[-self._max_samples :] for step, values in out.items()}

    def wall_by_step(self) -> dict[str, list[int]]:
        """Wall time (s) of the most recent `max_samples` runs per step, oldest first (P3-06:
        the driver's pilot-or-direct choice)."""
        out: dict[str, list[int]] = {}
        for sample in self.samples():
            out.setdefault(sample.step, []).append(sample.wall_s)
        return {step: values[-self._max_samples :] for step, values in out.items()}

    def _compact(self) -> None:
        with self.path.open("rb") as f:
            count = sum(1 for _ in f)
        if count <= self._max_lines:
            return
        samples = self.samples()
        totals = Counter(s.step for s in samples)
        seen: Counter[str] = Counter()
        lines: list[str] = []
        for sample in samples:  # keep the most recent per step, in their order
            seen[sample.step] += 1
            if seen[sample.step] > totals[sample.step] - self._max_samples:
                lines.append(json.dumps(sample.to_json(), sort_keys=True) + "\n")
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        os.replace(tmp, self.path)


def _parse(line: str) -> UsageSample | None:
    try:
        doc = json.loads(line)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    step = doc.get("step")
    values: list[int] = []
    for key in ("max_rss_kb", "cpu_s", "wall_s"):
        value = doc.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
        values.append(value)
    if not isinstance(step, str):
        return None
    return UsageSample(step, *values)


def p95(values: Sequence[int]) -> int:
    """The 95th percentile by nearest rank (a value that was observed)."""
    if not values:
        raise ValueError("p95 of no values")
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def format_mem(nbytes: int) -> str:
    """`16G`, `1536M`, `1.2G`: the largest unit with a value >= 1, exact or one decimal."""
    for unit, size in (("T", 1 << 40), ("G", 1 << 30), ("M", _MIB), ("K", _KIB)):
        if nbytes >= size:
            if nbytes % size == 0:
                return f"{nbytes // size}{unit}"
            return f"{nbytes / size:.1f}{unit}"
    return f"{nbytes}B"


@dataclass(frozen=True, slots=True)
class MemoryHint:
    step: str
    requested: int  # bytes, the largest request of the step's actions
    p95: int  # bytes
    samples: int
    suggested: int  # bytes

    def message(self) -> str:
        return (
            f"step {self.step!r} requests {format_mem(self.requested)} of memory but its last "
            f"{self.samples} successful runs used at most {format_mem(self.p95)} (p95); "
            f"consider resources.mem: {format_memory(self.suggested)}"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "step": self.step,
            "requested_mem": format_mem(self.requested),
            "p95_mem": format_mem(self.p95),
            "samples": self.samples,
            "suggested_mem": format_memory(self.suggested),
            "message": self.message(),
        }


def memory_hints(
    plan: Plan,
    usage: Mapping[str, Sequence[int]],
    *,
    factor: float = HINT_FACTOR,
    min_samples: int = MIN_SAMPLES,
) -> list[MemoryHint]:
    """Steps of `plan` whose memory request is more than `factor` x the p95 of `usage`
    (peak KiB per step, from `UsageLog.by_step`), in plan order."""
    requested: dict[str, int] = {}
    for spec in plan.actions:
        mem = spec.resources.mem
        if isinstance(mem, int):
            requested[spec.step] = max(requested.get(spec.step, 0), mem)
    hints: list[MemoryHint] = []
    for step, mem in requested.items():
        # 0 means "not measured" (no rusage), not "used nothing": such runs say nothing.
        observed = [kb for kb in usage.get(step, ()) if kb > 0]
        if len(observed) < min_samples:
            continue
        peak = p95(observed) * _KIB
        if mem <= factor * peak:
            continue
        suggested = max(_MIB, math.ceil(peak * SUGGEST_FACTOR / _MIB) * _MIB)
        hints.append(MemoryHint(step, mem, peak, len(observed), suggested))
    return hints


def record_usage(log: UsageLog, samples: Iterable[UsageSample]) -> None:
    """Best effort: a tuning log must never fail a build."""
    try:
        log.record(samples)
    except OSError as exc:
        _log.warning("driver.usage_not_recorded", path=str(log.path), error=str(exc))
