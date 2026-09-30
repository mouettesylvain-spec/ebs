"""Tests for `ebs.flow.matrix` (task P0-05 R3, R4, R8)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.digest import hash_bytes
from ebs.core.errors import FlowError
from ebs.flow.interp import Ref, SourceLocation
from ebs.flow.matrix import expand_matrix, load_matrix_tables, make_instance_id
from ebs.flow.model import StepDef
from ebs.flow.tables import Table

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "tables"


def _table(name: str, columns: Sequence[str], rows: Sequence[Sequence[str]]) -> Table:
    return Table(
        columns=tuple(columns),
        rows=tuple(MappingProxyType(dict(zip(columns, r, strict=True))) for r in rows),
        source=Path(name),
        digest=hash_bytes(name.encode()),
        lines=tuple(range(2, len(rows) + 2)),
    )


def _step(matrix: Mapping[str, Any] | None = None, **fields: Any) -> StepDef:
    data: dict[str, Any] = {"kind": "shell", **fields}
    if matrix is not None:
        data["matrix"] = matrix
    return StepDef.model_validate(data)


TESTS = _table("tests.csv", ["test"], [["smoke"], ["alu"]])
SEEDS = _table("seeds.csv", ["seed"], [["1"], ["2"]])


def _ids(step: StepDef, tables: Mapping[str, Table], name: str = "sim") -> list[str]:
    return [i.instance_id for i in expand_matrix(step, tables, name=name)]


# R3
def test_no_matrix_single_instance() -> None:
    (inst,) = expand_matrix(_step(params={"top": "tb"}), {}, name="elab")
    assert inst.instance_id == "elab"
    assert inst.name == "elab"
    assert dict(inst.row) == {}
    assert inst.row_origin is None
    # Unset resources stay unset, so the planner can still apply toolchain defaults.
    assert inst.resources.model_fields_set == set()
    assert dict(inst.params) == {"top": "tb"}


# R3
def test_table() -> None:
    step = _step(
        {"table": "regression.csv"},
        params={"test": "${row.test}", "seed": "${row.seed}", "plusargs": "${row.plusargs}"},
        env={"SEED": "${params.seed}"},
        resources={"cpus": 1, "mem": "4G", "time": "${row.timeout}"},
    )
    assert step.matrix is not None
    tables = load_matrix_tables(step.matrix, FIXTURES)
    instances = expand_matrix(step, tables, name="sim")
    assert [i.instance_id for i in instances] == [
        "sim[plusargs=,seed=1,test=smoke,timeout=10m]",
        "sim[plusargs=%2Bverbose,seed=2,test=smoke,timeout=10m]",
        "sim[plusargs=%2Ba%3D1%2C%2Bb%3D2,seed=17,test=alu_rand,timeout=1h]",
    ]
    last = instances[-1]
    assert dict(last.params) == {"test": "alu_rand", "seed": "17", "plusargs": "+a=1,+b=2"}
    assert dict(last.env) == {"SEED": "17"}
    assert (last.resources.cpus, last.resources.mem, last.resources.time) == (1, 4 << 30, 3600)
    assert last.step is step
    assert last.row_origin == "regression.csv:6"
    assert last.resources.model_fields_set == step.resources.model_fields_set


# R3
def test_cross() -> None:
    step = _step({"cross": ["tests.csv", "seeds.csv"]})
    instances = expand_matrix(step, {"tests.csv": TESTS, "seeds.csv": SEEDS}, name="sim")
    assert [dict(i.row) for i in instances] == [
        {"test": "smoke", "seed": "1"},
        {"test": "smoke", "seed": "2"},
        {"test": "alu", "seed": "1"},
        {"test": "alu", "seed": "2"},
    ]
    assert instances[1].row_origin == "tests.csv:2 + seeds.csv:3"


# R3
def test_cross_column_collision() -> None:
    other = _table("more.csv", ["seed", "test"], [["9", "x"]])
    step = _step({"cross": ["tests.csv", "more.csv"]})
    with pytest.raises(FlowError, match=r"column 'test'.*tests\.csv.*more\.csv"):
        expand_matrix(step, {"tests.csv": TESTS, "more.csv": other}, name="sim")


# R3
def test_zip() -> None:
    step = _step({"zip": ["tests.csv", "seeds.csv"]})
    rows = expand_matrix(step, {"tests.csv": TESTS, "seeds.csv": SEEDS}, name="sim")
    assert [dict(i.row) for i in rows] == [
        {"test": "smoke", "seed": "1"},
        {"test": "alu", "seed": "2"},
    ]


# R3
def test_zip_length_mismatch() -> None:
    three = _table("three.csv", ["seed"], [["1"], ["2"], ["3"]])
    step = _step({"zip": ["tests.csv", "three.csv"]})
    with pytest.raises(FlowError, match=r"same number of rows.*tests\.csv: 2.*three\.csv: 3"):
        expand_matrix(step, {"tests.csv": TESTS, "three.csv": three}, name="sim")


# R3
def test_filter() -> None:
    tables = {"tests.csv": TESTS, "seeds.csv": SEEDS}
    scalar = _step({"cross": ["tests.csv", "seeds.csv"], "filter": {"test": "alu"}})
    assert _ids(scalar, tables) == ["sim[seed=1,test=alu]", "sim[seed=2,test=alu]"]
    listed = _step({"cross": ["tests.csv", "seeds.csv"], "filter": {"seed": ["2", "7"]}})
    assert _ids(listed, tables) == ["sim[seed=2,test=smoke]", "sim[seed=2,test=alu]"]
    both = _step(
        {"cross": ["tests.csv", "seeds.csv"], "filter": {"seed": ["2"], "test": ["smoke"]}}
    )
    assert _ids(both, tables) == ["sim[seed=2,test=smoke]"]
    escaped = _table("e.csv", ["v"], [["${x}"], ["y"]])
    assert _ids(_step({"table": "e.csv", "filter": {"v": "$${x}"}}), {"e.csv": escaped}) == [
        "sim[v=%24%7Bx%7D]"
    ]


# R3, R4
@pytest.mark.parametrize(
    ("matrix", "message"),
    [
        ({"table": "tests.csv", "filter": {"tst": "x"}}, r"filter column 'tst'.*did you mean"),
        ({"table": "tests.csv", "filter": {"test": "none"}}, "no rows"),
        ({"table": "tests.csv", "filter": {"test": "${row.x}"}}, "literal"),
        ({"table": "tests.csv", "id": ["tset"]}, r"id column 'tset'.*available columns: test"),
        ({"table": "missing.csv"}, "not loaded"),
        ({"table": "tests.csv", "id": ["test", "test"]}, "id column 'test' is listed twice"),
    ],
)
def test_matrix_errors(matrix: Mapping[str, Any], message: str) -> None:
    with pytest.raises(FlowError, match=message):
        expand_matrix(_step(matrix), {"tests.csv": TESTS}, name="sim")


# R3
def test_empty_table_is_an_error() -> None:
    empty = _table("empty.csv", ["test"], [])
    with pytest.raises(FlowError, match="no rows"):
        expand_matrix(_step({"table": "empty.csv"}), {"empty.csv": empty}, name="sim")


# R5
def test_resolved_resource_errors() -> None:
    table = _table("t.csv", ["t", "c"], [["soon", "two"]])
    for resources, message in (
        ({"time": "${row.t}"}, r"resources\.time.*'soon'"),
        ({"cpus": "${row.c}"}, r"resources\.cpus.*'two'"),
        ({"mem": "${row.t}"}, r"resources\.mem.*'soon'"),
    ):
        step = _step({"table": "t.csv"}, resources=resources)
        with pytest.raises(FlowError, match=message):
            expand_matrix(step, {"t.csv": table}, name="sim")


# R6
def test_outer_resolver_receives_location() -> None:
    seen: list[SourceLocation] = []

    class Outer:
        def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
            seen.append(where)
            return ["a", "b"]

    step = _step(params={"libs": "${steps.compile.outputs.worklib[*]}"})
    (inst,) = expand_matrix(step, {}, name="elab", resolver=Outer())
    assert dict(inst.params) == {"libs": ("a", "b")}
    assert seen == [SourceLocation(step="elab", field="params.libs")]


# R5
def test_instance_render_uses_scope() -> None:
    step = _step({"table": "tests.csv"}, params={"t": "${row.test}"}, env={"E": "e"})
    first = expand_matrix(step, {"tests.csv": TESTS}, name="sim")[0]
    assert (
        first.render("run ${params.t} ${env.E} ${row.test}", field="script") == "run smoke e smoke"
    )


# R3
@pytest.mark.parametrize("path", ["/abs/t.csv", "../t.csv", "a/../../t.csv"])
def test_load_matrix_tables_rejects_escaping_paths(tmp_path: Path, path: str) -> None:
    step = _step({"table": path})
    assert step.matrix is not None
    with pytest.raises(FlowError, match="relative"):
        load_matrix_tables(step.matrix, tmp_path)


# R3
def test_load_matrix_tables_rejects_references(tmp_path: Path) -> None:
    step = _step({"table": "${row.x}.csv"})
    assert step.matrix is not None
    with pytest.raises(FlowError, match="literal path"):
        load_matrix_tables(step.matrix, tmp_path)


# R4
def test_make_instance_id_escaping() -> None:
    row = {"b": "x/y", "a": "1,2", "c": "[=]%", "d": ""}
    assert make_instance_id("sim", row, ("a", "b", "c", "d")) == (
        "sim[a=1%2C2,b=x%2Fy,c=%5B%3D%5D%25,d=]"
    )
    assert make_instance_id("sim", row, ("b",)) == "sim[b=x%2Fy]"
    assert make_instance_id("sim", {"k": "é"}, ("k",)) == "sim[k=%C3%A9]"


# R4
def test_id_narrowing() -> None:
    table = _table("r.csv", ["test", "seed", "note"], [["a", "1", "x"], ["a", "2", "x"]])
    step = _step({"table": "r.csv", "id": ["test", "seed"]})
    assert _ids(step, {"r.csv": table}) == ["sim[seed=1,test=a]", "sim[seed=2,test=a]"]


# R4
def test_duplicate_rows_error() -> None:
    table = _table(
        "r.csv", ["test", "seed"], [["a", "1"], ["b", "1"], ["a", "2"], ["a", "3"], ["b", "4"]]
    )
    step = _step({"table": "r.csv", "id": ["test"]})
    with pytest.raises(FlowError) as exc:
        expand_matrix(step, {"r.csv": table}, name="sim")
    message = str(exc.value)
    assert "sim[test=a] at r.csv:2, r.csv:4, r.csv:5" in message
    assert "sim[test=b] at r.csv:3, r.csv:6" in message
    assert "matrix.id" in message  # tells the user how to fix it


# R4
def test_duplicate_full_rows_error() -> None:
    table = _table("r.csv", ["test"], [["a"], ["a"]])
    with pytest.raises(FlowError, match="duplicate"):
        expand_matrix(_step({"table": "r.csv"}), {"r.csv": table}, name="sim")


_VALUES = st.text(alphabet="ab1,=[]/%$é _-", max_size=4)
_COLUMNS = st.lists(st.sampled_from(["test", "seed", "x", "y_1"]), min_size=1, unique=True)


@st.composite
def _tables(draw: st.DrawFn) -> Table:
    columns = draw(_COLUMNS)
    rows = draw(
        st.lists(st.tuples(*[_VALUES for _ in columns]), min_size=1, max_size=8, unique=True)
    )
    return _table("h.csv", columns, rows)


# R4
@given(_tables())
def test_instance_id_stable_unique(table: Table) -> None:
    step = _step({"table": "h.csv"})
    ids = _ids(step, {"h.csv": table})
    assert len(set(ids)) == len(ids) == len(table.rows)
    assert ids == _ids(step, {"h.csv": table})
    for iid in ids:  # safe as a single path component
        assert "/" not in iid


# R8
@given(_tables(), st.randoms(use_true_random=False))
def test_deterministic_order(table: Table, rnd: Any) -> None:
    step = _step({"table": "h.csv"}, params={"p": "${row." + table.columns[0] + "}"})
    first = expand_matrix(step, {"h.csv": table}, name="sim")
    assert [dict(i.row) for i in first] == [dict(r) for r in table.rows]  # table order
    # Reordering the columns of each row mapping must not change ids or order.
    shuffled_rows = []
    for row in table.rows:
        items = list(row.items())
        rnd.shuffle(items)
        shuffled_rows.append(MappingProxyType(dict(items)))
    shuffled = Table(table.columns, tuple(shuffled_rows), table.source, table.digest, table.lines)
    second = expand_matrix(step, {"h.csv": shuffled}, name="sim")
    assert [i.instance_id for i in first] == [i.instance_id for i in second]
    assert [dict(i.params) for i in first] == [dict(i.params) for i in second]
