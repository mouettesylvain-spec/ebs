"""Helpers for the Questa rule pack tests (task P1-08): a resolver that stages fan-in outputs the
way the planner does (`<output>/<producer action id>`), and script parsing."""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ebs.core.digest import hash_bytes
from ebs.flow.interp import Ref, SourceLocation
from ebs.flow.matrix import expand_matrix
from ebs.flow.model import StepDef
from ebs.flow.tables import Table
from ebs.rules.api import ExpandContext
from tests.helpers.rules import instance

FAKES_BIN = Path(__file__).resolve().parents[1] / "fakes" / "bin"


@dataclass
class StagedResolver:
    """`${steps.S.outputs.O…}` -> staged paths; `producers[(S, O)]` lists the producer ids."""

    producers: Mapping[tuple[str, str], Sequence[str]] = field(default_factory=dict)

    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
        if ref.kind == "imports":  # an imported release, staged like a source tree
            return f"imp/{ref.name}" + (f"/{ref.glob}" if ref.glob else "")
        assert ref.kind == "steps", ref
        assert ref.output is not None, ref
        paths = [f"{ref.output}/{pid}" for pid in self.producers[(ref.name, ref.output)]]
        return paths if ref.selector == "*" else paths[0]


def qcontext(
    step_def: StepDef, resolver: StagedResolver | None = None, *, name: str = "s"
) -> ExpandContext:
    return ExpandContext(instance(step_def, name), resolver or StagedResolver())


def qcontexts(
    step_def: StepDef,
    rows: Sequence[Mapping[str, str]],
    *,
    name: str,
    resolver: StagedResolver | None = None,
) -> list[ExpandContext]:
    """One context per matrix row (`step_def` must have `matrix: {table: t.csv}`)."""
    table = Table(
        columns=tuple(rows[0]),
        rows=tuple(MappingProxyType(dict(r)) for r in rows),
        source=Path("t.csv"),
        digest=hash_bytes(b"t"),
        lines=tuple(range(2, 2 + len(rows))),
    )
    return [
        ExpandContext(inst, resolver or StagedResolver())
        for inst in expand_matrix(step_def, {"t.csv": table}, name=name)
    ]


def script_commands(script: str) -> list[list[str]]:
    """The commands of a generated bash script, each split as bash would (comments skipped)."""
    return [shlex.split(line) for line in script.splitlines() if line and not line.startswith("#")]
