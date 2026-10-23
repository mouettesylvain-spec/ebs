"""Comparators: is a re-run's result the same as the archived one? (P3-05 R2)

A revalidation (`ebs.release.revalidate`) re-runs a sign-off plan, typically on newer tools, so
output bytes are expected to differ; a comparator decides whether the *meaning* did. It gets the
outputs of one action from both runs, materialized on local disk (output name -> path; an
output that could not be materialized is missing), and both result-manifest summaries, and
returns a `Verdict`:

- `equivalent`: the same outcome;
- `changed(details)`: a difference worth a human's look, one line per difference;
- `incomparable(reason)`: the two runs cannot be compared (missing result, other test, …).

Comparators are named (`sim`, `lint`) and built from their `[revalidate.comparators.<name>]`
settings with `get_comparator`; which step uses which is the revalidation's choice (by step
name, else by rule kind). They follow the rule plugin import rules: core, flow, rules.api only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

__all__ = [
    "Comparator",
    "ComparatorFactory",
    "Summary",
    "Verdict",
    "VerdictKind",
    "comparator_names",
    "get_comparator",
]

VerdictKind = Literal["equivalent", "changed", "incomparable"]
Summary = Mapping[str, str | int]
"""A result manifest's `summary` (`RulePlugin.summarize`)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    kind: VerdictKind
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind == "equivalent" and self.details:
            raise ValueError("an equivalent verdict has no details")
        if self.kind != "equivalent" and not self.details:
            raise ValueError(f"a {self.kind} verdict needs at least one detail line")

    @classmethod
    def equivalent(cls) -> Verdict:
        return cls("equivalent")

    @classmethod
    def changed(cls, *details: str) -> Verdict:
        return cls("changed", tuple(details))

    @classmethod
    def incomparable(cls, reason: str) -> Verdict:
        return cls("incomparable", (reason,))


class Comparator(Protocol):
    name: str

    def compare(
        self,
        old_outputs: Mapping[str, Path],
        new_outputs: Mapping[str, Path],
        old_summary: Summary,
        new_summary: Summary,
    ) -> Verdict: ...


ComparatorFactory = Callable[[Mapping[str, object]], Comparator]


def _factories() -> dict[str, ComparatorFactory]:
    from ebs.rules.comparators.lint import LintComparator
    from ebs.rules.comparators.sim import SimComparator

    return {"sim": SimComparator.from_settings, "lint": LintComparator.from_settings}


def comparator_names() -> tuple[str, ...]:
    return tuple(sorted(_factories()))


def get_comparator(name: str, settings: Mapping[str, object] | None = None) -> Comparator:
    """The comparator `name`, built from its settings; ValueError for an unknown name or a bad
    setting (callers turn it into a ConfigError naming the config key)."""
    factory = _factories().get(name)
    if factory is None:
        raise ValueError(f"unknown comparator {name!r}; available: {', '.join(comparator_names())}")
    return factory(settings or {})
