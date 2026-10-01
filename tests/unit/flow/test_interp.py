"""Tests for `ebs.flow.interp` (task P0-05 R5, R6, R7 and the parser round-trip)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.errors import FlowError
from ebs.flow.interp import (
    Literal,
    Ref,
    Resolver,
    Scope,
    SourceLocation,
    parse_template,
    render,
)
from ebs.flow.model import check_ref_syntax

WHERE = SourceLocation(step="sim", field="params.test")


class ListResolver:
    """Outer resolver standing in for the planner: `steps.*` refs are lists, imports a string."""

    def __init__(self) -> None:
        self.calls: list[tuple[Ref, SourceLocation]] = []

    def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
        self.calls.append((ref, where))
        if ref.kind == "steps":
            return ["work/a", "work/b"]
        return "rtl"


def _scope(
    row: Mapping[str, str] | None = None,
    params: Mapping[str, str | int | bool] | None = None,
    env: Mapping[str, str] | None = None,
    outer: Resolver | None = None,
) -> Scope:
    return Scope(step="sim", row=row, params=params or {}, env=env or {}, outer=outer)


def _render(text: str, scope: Resolver) -> str | list[str]:
    return render(parse_template(text), scope, where=WHERE)


# R5
def test_parse_parts() -> None:
    t = parse_template("vsim ${params.top} -sv_seed ${row.seed}")
    assert t.parts == (
        Literal("vsim "),
        Ref(kind="params", name="top"),
        Literal(" -sv_seed "),
        Ref(kind="row", name="seed"),
    )
    assert parse_template("").parts == ()


# R5
def test_parse_ref_fields() -> None:
    (imp,) = parse_template("${imports.rtl/**/*.sv}").parts
    assert imp == Ref(kind="imports", name="rtl", glob="**/*.sv")
    (star,) = parse_template("${steps.compile.outputs.worklib[*]}").parts
    assert star == Ref(kind="steps", name="compile", output="worklib", selector="*")
    (sel,) = parse_template("${steps.compile.outputs.worklib[lib=alu,x=1]}").parts
    assert sel == Ref(
        kind="steps", name="compile", output="worklib", selector=(("lib", "alu"), ("x", "1"))
    )
    (plain,) = parse_template("${steps.elab.outputs.model}").parts
    assert plain == Ref(kind="steps", name="elab", output="model")
    for ref in (imp, star, sel, plain):
        assert parse_template(ref.text).parts == (ref,)


# R5
@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("${row.x", "unterminated reference"),
        ("${rows.x}", "did you mean 'row'"),
        ("${param.x}", "did you mean 'params'"),
        ("${row.}", "malformed reference"),
        ("${steps.S.outputs.o}", "malformed reference"),
        ("${steps.s.outputs.o[a=]}", "malformed reference"),
    ],
)
def test_parse_errors(text: str, message: str) -> None:
    with pytest.raises(FlowError, match=message):
        parse_template(text)


# R5
def test_row_param_env_resolution() -> None:
    scope = _scope(
        row={"test": "smoke", "seed": "17"},
        params={"top": "tb_top", "n": 3, "flag": True, "tag": "${params.top}-${row.seed}"},
        env={"MODE": "${params.tag}", "PLAIN": "x"},
    )
    assert _render("${row.test}:${row.seed}", scope) == "smoke:17"
    assert _render("${params.tag}", scope) == "tb_top-17"
    assert _render("n=${params.n} f=${params.flag}", scope) == "n=3 f=true"
    assert _render("${env.MODE}", scope) == "tb_top-17"
    assert scope.params() == {"top": "tb_top", "n": 3, "flag": True, "tag": "tb_top-17"}
    assert scope.env() == {"MODE": "tb_top-17", "PLAIN": "x"}


# R5
def test_missing_column_message() -> None:
    scope = _scope(row={"test": "smoke", "seed": "17"})
    with pytest.raises(FlowError) as exc:
        _render("${row.tst}", scope)
    message = str(exc.value)
    assert "no column 'tst'" in message
    assert "available columns: seed, test" in message
    assert "did you mean 'test'" in message
    assert "steps.sim.params.test" in message  # where


# R5
def test_row_ref_without_matrix() -> None:
    with pytest.raises(FlowError, match="has no matrix"):
        _render("${row.test}", _scope(row=None))


# R5
def test_unknown_param() -> None:
    with pytest.raises(FlowError, match=r"no param 'topp'.*did you mean 'top'"):
        _render("${params.topp}", _scope(params={"top": "t"}))


# R5
def test_param_cycle() -> None:
    scope = _scope(params={"a": "${params.b}", "b": "x${params.a}"})
    with pytest.raises(FlowError, match=r"params\.a -> params\.b -> params\.a"):
        scope.params()


# R5
def test_env_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNDECLARED", "from-process")
    scope = _scope(env={"DECLARED": "yes"})
    assert _render("${env.DECLARED}", scope) == "yes"
    with pytest.raises(FlowError) as exc:
        _render("${env.UNDECLARED}", scope)
    assert "not declared" in str(exc.value)
    assert "DECLARED" in str(exc.value)  # lists the declared env
    with pytest.raises(FlowError, match="did you mean 'DECLARED'"):
        _render("${env.DECLAREDD}", scope)
    assert "from-process" not in str(exc.value)
    # Another step's scope does not see this step's env.
    with pytest.raises(FlowError, match="not declared"):
        _render("${env.DECLARED}", _scope(env={}))


# R5, R6
def test_env_must_be_string() -> None:
    scope = _scope(env={"LIBS": "${steps.c.outputs.w[*]}"}, outer=ListResolver())
    with pytest.raises(FlowError, match=r"env\.LIBS"):
        scope.env()


# R5
def test_escape() -> None:
    scope = _scope(row={"x": "1"})
    assert parse_template("$${row.x}").parts == (Literal("${row.x}"),)
    assert _render("$${row.x}", scope) == "${row.x}"
    assert _render("echo $${HOME} ${row.x}", scope) == "echo ${HOME} 1"
    assert _render("$$ $x {y} $", scope) == "$$ $x {y} $"
    assert _render("$$${row.x}", scope) == "$${row.x}"


# R6
def test_list_ref_rules() -> None:
    outer = ListResolver()
    scope = _scope(row={"x": "1"}, outer=outer)
    assert _render("${steps.c.outputs.w[*]}", scope) == ["work/a", "work/b"]
    assert _render("${imports.rtl}", scope) == "rtl"
    assert _render("${row.x}", scope) == "1"  # a single string ref stays a string
    for mixed in ("-L ${steps.c.outputs.w[*]}", "${steps.c.outputs.w[*]}${row.x}"):
        with pytest.raises(FlowError, match="list"):
            _render(mixed, scope)
    ref, where = outer.calls[0]
    assert ref == Ref(kind="steps", name="c", output="w", selector="*")
    assert where == WHERE


# R6
def test_list_param_passes_through() -> None:
    scope = _scope(params={"libs": "${steps.c.outputs.w[*]}"}, outer=ListResolver())
    assert scope.params() == {"libs": ("work/a", "work/b")}
    with pytest.raises(FlowError, match="list"):
        _render("-L ${params.libs}", scope)


# R5
def test_planner_refs_need_outer_resolver() -> None:
    with pytest.raises(FlowError, match="resolved by the planner"):
        _render("${steps.c.outputs.w}", _scope())


# R7
def test_no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EBS_SENTINEL", "leaked-value")
    monkeypatch.setenv("HOME", "/home/leaked-value")
    scope = _scope(row={"v": "${env.EBS_SENTINEL}", "code": "__import__('os')"})
    for text in ("${HOME}", "${EBS_SENTINEL}", "${os.environ}"):
        with pytest.raises(FlowError) as exc:
            _render(text, scope)
        assert "leaked-value" not in str(exc.value)
    with pytest.raises(FlowError, match="not declared"):
        _render("${env.EBS_SENTINEL}", scope)
    # Shell-style references are plain text; values are inserted verbatim, never re-expanded.
    assert _render("$HOME ~ $EBS_SENTINEL", scope) == "$HOME ~ $EBS_SENTINEL"
    assert _render("${row.v}", scope) == "${env.EBS_SENTINEL}"
    assert _render("${row.code}", scope) == "__import__('os')"
    with pytest.raises(FlowError, match="no column '__class__'"):
        _render("${row.__class__}", scope)


_CHUNKS = st.sampled_from(
    [
        "${row.a}",
        "${params.p_1}",
        "${env.HOME}",
        "${imports.rtl/**/*.sv}",
        "${steps.s.outputs.o[*]}",
        "${steps.s.outputs.o[k=v,j=2]}",
        "$${",
        "$$",
        "$",
        "{",
        "}",
        "a",
        " ",
        "é",
    ]
)


# Done when: parser round-trip
@given(st.lists(_CHUNKS, max_size=12).map("".join))
def test_roundtrip(text: str) -> None:
    try:
        template = parse_template(text)
    except FlowError:
        return
    assert parse_template(str(template)) == template


# R5: grammar matches P0-04 check_ref_syntax
@given(st.one_of(st.lists(_CHUNKS, max_size=8).map("".join), st.text(max_size=20)))
def test_agrees_with_model_syntax_check(text: str) -> None:
    valid = check_ref_syntax(text) is None
    try:
        parse_template(text)
    except FlowError:
        assert not valid
    else:
        assert valid
