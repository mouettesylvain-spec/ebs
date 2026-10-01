"""Tests for the `make` rule (task P0-12 R3, R5, R6)."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.digest import hash_bytes
from ebs.core.errors import RuleError
from ebs.flow.matrix import expand_matrix
from ebs.flow.tables import Table
from ebs.rules.api import Classification, ExpandContext, expand_runtime_env
from ebs.rules.make import MakeRule
from tests.helpers.rules import CLASSIFY_CASES, context, step


# R3
def test_argv_and_vars_sorted() -> None:
    s = step(
        kind="make",
        workdir="flows/lint",
        target="lint",
        params={"top": "cpu", "OPTS": "-x -y", "count": 3, "fast": True},
        inputs={"rtl": "rtl/**/*.sv"},
        outputs={"reports": {"dir": "reports/lint"}},
    )
    t = MakeRule().expand(s, context(s))
    assert t.argv == (
        "make",
        "-C",
        "flows/lint",
        "lint",
        "OPTS=-x -y",
        "count=3",
        "fast=true",
        "top=cpu",
    )
    # The whole workdir tree is an implicit input (declared inputs stay with the planner).
    assert dict(t.inputs) == {"ebs.workdir": "flows/lint/**"}
    # Declared output dirs are the outputs; the rule adds none.
    assert dict(t.outputs) == {}


# R3
@given(
    st.dictionaries(st.from_regex(r"[a-z_][a-z0-9_]{0,8}", fullmatch=True), st.text(), max_size=6)
)
def test_vars_sorted_property(params: dict[str, str]) -> None:
    s = step(kind="make", workdir=".", target="all", params=params)
    t = MakeRule().expand(s, context(s))
    assert t.argv[:4] == ("make", "-C", ".", "all")
    assert list(t.argv[4:]) == [f"{k}={params[k]}" for k in sorted(params)]
    assert dict(t.inputs) == {"ebs.workdir": "**"}


# R3
def test_workdir_normalized_and_target_optional() -> None:
    s = step(kind="make", workdir="./flows//lint/")
    t = MakeRule().expand(s, context(s))
    assert t.argv == ("make", "-C", "flows/lint")
    assert dict(t.inputs) == {"ebs.workdir": "flows/lint/**"}


# R3
@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "'workdir' is required"),
        ({"workdir": "a", "script": "true"}, "'script'"),
        ({"workdir": "a", "command": ["make"]}, "'command'"),
        ({"workdir": "a", "params": {"MAKEFLAGS": "-j4"}}, "MAKEFLAGS"),
        ({"workdir": "a", "params": {"MAKELEVEL": "1"}}, "MAKELEVEL"),
    ],
)
def test_validate_errors(fields: dict[str, object], message: str) -> None:
    with pytest.raises(RuleError, match=message):
        MakeRule().validate(step(kind="make", **fields))


# R3: workdir and target are checked after interpolation, not only as written.
@pytest.mark.parametrize(
    ("workdir", "target", "message"),
    [
        ("${row.w}", "all", "must be relative"),
        ("x/${row.w}", "all", "must be relative"),
        ("a", "${row.t}", "target '-j99' must not"),
        ("a", "${row.v}", "target 'X=1' must not.*contain '='"),
    ],
)
def test_rendered_paths_checked(workdir: str, target: str, message: str) -> None:
    row = {"w": "../../etc", "t": "-j99", "v": "X=1"}
    table = Table(
        columns=tuple(row),
        rows=(MappingProxyType(row),),
        source=Path("t.csv"),
        digest=hash_bytes(b"t"),
        lines=(2,),
    )
    s = step(kind="make", workdir=workdir, target=target, matrix={"table": "t.csv"})
    (inst,) = expand_matrix(s, {"t.csv": table}, name="s")
    with pytest.raises(RuleError, match=message):
        MakeRule().expand(s, ExpandContext(inst))


# R3: -j comes from resources.cpus via runtime env, never from keyed fields.
def test_jobs_not_in_key() -> None:
    def keyed(cpus: int) -> tuple[object, ...]:
        s = step(kind="make", workdir="flows/lint", target="lint", resources={"cpus": cpus})
        t = MakeRule().expand(s, context(s))
        assert not any(a.startswith("-j") for a in t.argv)
        assert "MAKEFLAGS" not in t.env
        return (t.argv, dict(t.env), dict(t.inputs), dict(t.outputs), dict(t.config_files))

    assert keyed(1) == keyed(16)
    s = step(kind="make", workdir="flows/lint", target="lint", resources={"cpus": 16})
    t = MakeRule().expand(s, context(s))
    assert expand_runtime_env(t.runtime_env, {"EBS_CPUS": "16"}) == {"MAKEFLAGS": "-j16"}


# R3
def test_makeflags_scrubbed() -> None:
    s = step(
        kind="make",
        workdir="flows/lint",
        env={"MAKEFLAGS": "-j99 -k", "MFLAGS": "-k", "MAKELEVEL": "2", "FOO": "bar"},
    )
    t = MakeRule().expand(s, context(s))
    assert dict(t.env) == {"FOO": "bar"}


# R5
@pytest.mark.parametrize(("exit_code", "log_tail", "expected"), CLASSIFY_CASES)
def test_classify(exit_code: int, log_tail: str, expected: Classification) -> None:
    assert MakeRule().classify(exit_code, log_tail, {}) == expected


GOLDEN = {
    "version": "1",
    "argv": ["make", "-C", "flows/lint", "lint", "RULESET=strict", "TOP=cpu_top"],
    "inputs": {"ebs.workdir": "flows/lint/**"},
    "runtime_env": {"MAKEFLAGS": "-j$EBS_CPUS"},
}


# R6
def test_golden_argv() -> None:
    rule = MakeRule()
    s = step(
        kind="make",
        workdir="flows/lint",
        target="lint",
        params={"TOP": "cpu_top", "RULESET": "strict"},
        outputs={"reports": {"dir": "reports/lint"}},
    )
    t = rule.expand(s, context(s))
    actual = {
        "version": rule.version,
        "argv": list(t.argv),
        "inputs": dict(t.inputs),
        "runtime_env": dict(t.runtime_env),
    }
    assert actual == GOLDEN, (
        "the make rule's generated command changed: if intended, bump MakeRule.version "
        "(it is part of every action key) and update GOLDEN in this test"
    )
