"""Matrix expansion: one `StepInstance` per table row with row/params/env/resources resolved.

Modes (task P0-05 R3): `table` (one instance per row), `cross` (cartesian product, first table
outermost), `zip` (row i of every table). `filter` keeps rows whose column value is listed.
Instances come out in table order, and each gets a stable id such as `sim[seed=17,test=smoke]`
(key columns sorted, values percent-encoded so the id holds no `/` and is injective; it can
still exceed a filesystem's NAME_MAX, so callers must not use it as a file name as-is).
"""

from __future__ import annotations

import difflib
import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote

from ebs.core.errors import FlowError
from ebs.flow.interp import (
    Literal,
    ParamValue,
    Resolver,
    Scope,
    parse_template,
)
from ebs.flow.model import MatrixDef, Resources, StepDef, parse_duration, parse_memory
from ebs.flow.tables import Table, load_table

__all__ = ["StepInstance", "expand_matrix", "load_matrix_tables", "make_instance_id"]

_CPUS_RE = re.compile(r"[1-9][0-9]*", re.ASCII)


@dataclass(frozen=True)
class StepInstance:
    """One concrete instance of a step: its matrix row and its resolved params, env, resources.

    `row` is empty and `row_origin` None for a step without a matrix. `render` resolves any other
    template of the step (command, inputs, outputs…) in this instance's scope.
    """

    name: str
    step: StepDef
    row: Mapping[str, str]
    instance_id: str
    params: Mapping[str, ParamValue]
    env: Mapping[str, str]
    resources: Resources
    row_origin: str | None
    scope: Scope = field(repr=False, compare=False)

    def render(self, text: str, *, field: str, resolver: Resolver | None = None) -> str | list[str]:
        """Render `text` (from the step's `field`), with `resolver` for imports/steps refs."""
        scope = self.scope if resolver is None else self.scope.with_outer(resolver)
        return scope.render(text, field=field)


def make_instance_id(name: str, row: Mapping[str, str], keys: Sequence[str]) -> str:
    """`name[k1=v1,k2=v2]` over `keys` sorted; keys and values are percent-encoded (UTF-8)."""
    pairs = ",".join(f"{quote(k, safe='')}={quote(row[k], safe='')}" for k in sorted(keys))
    return f"{name}[{pairs}]"


def load_matrix_tables(matrix: MatrixDef, base: Path) -> dict[str, Table]:
    """Load the tables a matrix names, relative to `base`, keyed by the path as written."""
    tables: dict[str, Table] = {}
    for written in matrix.tables:
        template = parse_template(written)
        if template.refs:
            raise FlowError(
                f"matrix table {written!r} must be a literal path; references are not allowed"
            )
        literal = "".join(p.text for p in template.parts if isinstance(p, Literal))
        parts = literal.split("/")
        if literal.startswith("/") or ".." in parts:
            raise FlowError(
                f"matrix table {written!r} must be a relative path inside the flow directory "
                "(no leading '/', no '..')"
            )
        tables[written] = load_table(base / literal)
    return tables


@dataclass(frozen=True)
class _Row:
    values: dict[str, str]
    origin: str


def _error(name: str, message: str) -> FlowError:
    return FlowError(f"steps.{name}.matrix: {message}")


def _suggest(value: str, choices: Sequence[str]) -> str:
    close = difflib.get_close_matches(value, choices, n=1, cutoff=0.6)
    return f"; did you mean {close[0]!r}?" if close else ""


def _combine(
    name: str, matrix: MatrixDef, tables: Mapping[str, Table]
) -> tuple[list[str], list[_Row]]:
    selected: list[tuple[str, Table]] = []
    for path in matrix.tables:
        if path not in tables:
            raise _error(name, f"table {path!r} was not loaded (have: {sorted(tables)})")
        selected.append((path, tables[path]))
    columns: list[str] = []
    owner: dict[str, str] = {}
    for path, table in selected:
        for column in table.columns:
            if column in owner:
                raise _error(
                    name,
                    f"column {column!r} appears in both {owner[column]!r} and {path!r}; "
                    "rename it in one of the tables",
                )
            owner[column] = path
            columns.append(column)
    if matrix.zip_ is not None:
        lengths = {len(t.rows) for _, t in selected}
        if len(lengths) > 1:
            counts = ", ".join(f"{p}: {len(t.rows)}" for p, t in selected)
            raise _error(name, f"zip needs tables with the same number of rows ({counts})")
        indexes: list[tuple[int, ...]] = [(i,) * len(selected) for i in range(lengths.pop())]
    else:  # table or cross; product of one table is its rows
        indexes = list(itertools.product(*(range(len(t.rows)) for _, t in selected)))
    rows = []
    for combo in indexes:
        values: dict[str, str] = {}
        for (_, table), i in zip(selected, combo, strict=True):
            values.update(table.rows[i])
        origin = " + ".join(t.origin(i) for (_, t), i in zip(selected, combo, strict=True))
        rows.append(_Row(values, origin))
    return columns, rows


def _filter(name: str, matrix: MatrixDef, columns: list[str], rows: list[_Row]) -> list[_Row]:
    for column, wanted in matrix.filter.items():
        if column not in columns:
            raise _error(
                name,
                f"filter column {column!r} is not a matrix column{_suggest(column, columns)}; "
                f"available columns: {', '.join(sorted(columns))}",
            )
        allowed: set[str] = set()
        for text in (wanted,) if isinstance(wanted, str) else wanted:
            template = parse_template(text)
            if template.refs:
                raise _error(name, f"filter value {text!r} for {column!r} must be literal")
            allowed.add("".join(p.text for p in template.parts if isinstance(p, Literal)))
        rows = [r for r in rows if r.values[column] in allowed]
    return rows


def _check_unique(name: str, matrix: MatrixDef, ids: list[str], rows: list[_Row]) -> None:
    groups: dict[str, list[str]] = {}
    for iid, row in zip(ids, rows, strict=True):
        groups.setdefault(iid, []).append(row.origin)
    duplicates = [(iid, origins) for iid, origins in groups.items() if len(origins) > 1]
    if not duplicates:
        return
    listing = "; ".join(f"{iid} at {', '.join(origins)}" for iid, origins in duplicates)
    if matrix.id is not None:
        fix = (
            f"add columns to 'matrix.id' (now: {', '.join(matrix.id)}) so every row gets a "
            "distinct id, or remove the duplicate rows"
        )
    else:
        fix = "remove the duplicate rows (they are identical in every column)"
    raise _error(name, f"rows are not unique, so instance ids collide: {listing}; {fix}")


def _resources(name: str, resources: Resources, scope: Scope) -> Resources:
    def text(value: str, attr: str) -> str:
        rendered = scope.render(value, field=f"resources.{attr}")
        if isinstance(rendered, list):
            raise FlowError(f"steps.{name}.resources.{attr}: resolved to a list; expected a value")
        return rendered

    def fail(attr: str, detail: str) -> FlowError:
        return FlowError(f"steps.{name}.resources.{attr}: {detail}")

    cpus, mem, time = resources.cpus, resources.mem, resources.time
    if isinstance(cpus, str):
        value = text(cpus, "cpus")
        if _CPUS_RE.fullmatch(value) is None:
            raise fail("cpus", f"expected a positive integer, got {value!r}")
        cpus = int(value)
    try:
        if isinstance(mem, str):
            mem = parse_memory(text(mem, "mem"))
    except ValueError as exc:
        raise fail("mem", str(exc)) from None
    try:
        if isinstance(time, str):
            time = parse_duration(text(time, "time"))
    except ValueError as exc:
        raise fail("time", str(exc)) from None
    # Values are already parsed; validation would reject the byte/second ints as bare numbers.
    return Resources.model_construct(
        _fields_set=set(resources.model_fields_set), cpus=cpus, mem=mem, time=time
    )


def expand_matrix(
    step: StepDef,
    tables: Mapping[str, Table],
    *,
    name: str,
    resolver: Resolver | None = None,
) -> list[StepInstance]:
    """Expand `step` (called `name` in the flow) into instances, in table order.

    `tables` maps each path as written in `step.matrix` to its loaded table
    (`load_matrix_tables`). `resolver` handles `${imports.*}` and `${steps.*}` in params, env
    and resources; without it those references are errors.
    """
    matrix = step.matrix
    if matrix is None:
        rows: list[_Row | None] = [None]
        ids = [name]
    else:
        columns, combined = _combine(name, matrix, tables)
        combined = _filter(name, matrix, columns, combined)
        if not combined:
            reason = " after 'filter'" if matrix.filter else ""
            raise _error(name, f"expansion produced no rows{reason}; the step would never run")
        keys = matrix.id if matrix.id is not None else tuple(columns)
        for key in keys:
            if keys.count(key) > 1:
                raise _error(name, f"id column {key!r} is listed twice in 'matrix.id'")
            if key not in columns:
                raise _error(
                    name,
                    f"id column {key!r} is not a matrix column{_suggest(key, columns)}; "
                    f"available columns: {', '.join(sorted(columns))}",
                )
        ids = [make_instance_id(name, r.values, keys) for r in combined]
        _check_unique(name, matrix, ids, combined)
        rows = list(combined)
    instances = []
    for iid, row in zip(ids, rows, strict=True):
        values = MappingProxyType(row.values) if row is not None else MappingProxyType({})
        scope = Scope(
            step=name,
            row=values if row is not None else None,
            params=step.params,
            env=step.env,
            outer=resolver,
        )
        instances.append(
            StepInstance(
                name=name,
                step=step,
                row=values,
                instance_id=iid,
                params=MappingProxyType(scope.params()),
                env=MappingProxyType(scope.env()),
                resources=_resources(name, step.resources, scope),
                row_origin=row.origin if row is not None else None,
                scope=scope,
            )
        )
    return instances
