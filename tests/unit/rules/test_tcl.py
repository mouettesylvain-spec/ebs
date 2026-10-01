"""Tests for the `tcl` rule (task P0-12 R4, R5, R6).

Quoting is checked against a real Tcl interpreter: the stdlib `tkinter.Tcl()` (no display needed)
for the property test, and a `tclsh` on PATH for the end-to-end run.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.core.errors import RuleError
from ebs.flow.model import StepDef
from ebs.rules.api import ActionTemplate, Classification
from ebs.rules.tcl import TclRule, tcl_quote
from tests.helpers.rules import CLASSIFY_CASES, context, step

PARAMS_FILE = ".ebs/ebs_params.tcl"
MAIN_FILE = ".ebs/ebs_main.tcl"

# Characters Tcl treats specially, plus control characters (\x1a is `source`'s EOF character).
_TRICKY = '{}[]$\\"; \t\n\r#\x00\x1a\x7f é€😀'
_values = st.text(
    alphabet=st.one_of(st.sampled_from(_TRICKY), st.characters(exclude_categories=["Cs"])),
    max_size=30,
)


@functools.cache
def _interp() -> Any:
    import tkinter

    return tkinter.Tcl()


def _tcl() -> Any:
    tkinter = pytest.importorskip("tkinter", reason="needs the stdlib tkinter module (Tcl)")
    try:
        return _interp()
    except tkinter.TclError as exc:  # pragma: no cover - depends on the host
        pytest.skip(f"tkinter cannot start a Tcl interpreter: {exc}")


def _write(tmp_path: Path, template: ActionTemplate) -> None:
    for rel, content in template.config_files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _expand(
    params: dict[str, Any], command: tuple[str, ...] = ("tclsh", "run.tcl")
) -> ActionTemplate:
    s = StepDef.model_validate({"kind": "tcl", "command": list(command), "params": params})
    return TclRule().expand(s, context(s))


_HEX = "binary encode hex [encoding convertto utf-8 $::ebs({name})]"


# R4
def test_params_file_format() -> None:
    t = _expand({"lib": "core", "n": 4, "fast": False, "opts": "-a {b}"})
    assert t.config_files[PARAMS_FILE].splitlines()[1:] == [
        "array set ::ebs {}",
        "set ::ebs(fast) {false}",
        "set ::ebs(lib) {core}",
        "set ::ebs(n) {4}",
        "set ::ebs(opts) {-a {b}}",
    ]
    assert t.config_files[PARAMS_FILE].isascii()


# R4
@given(
    st.dictionaries(
        st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,6}", fullmatch=True), _values, max_size=4
    )
)
def test_params_file_quoting(params: dict[str, str]) -> None:
    tcl = _tcl()
    t = _expand(params)
    text = t.config_files[PARAMS_FILE]
    assert text.isascii()  # independent of the tool's system encoding
    tcl.eval("array unset ::ebs")
    tcl.eval(text)
    assert tcl.eval("lsort [array names ::ebs]").split() == sorted(params)
    for name, value in params.items():
        assert tcl.eval(_HEX.format(name=name)) == value.encode().hex(), repr(value)


# R4
@given(_values)
def test_quote_roundtrip(value: str) -> None:
    tcl = _tcl()
    tcl.eval(f"set ::x {tcl_quote(value)}")
    assert tcl.eval("binary encode hex [encoding convertto utf-8 $::x]") == value.encode().hex()


# R4
@given(st.lists(_values, max_size=4))
def test_list_param_is_tcl_list(items: list[str]) -> None:
    from ebs.rules.tcl import params_file

    tcl = _tcl()
    tcl.eval("array unset ::ebs")
    tcl.eval(params_file({"files": tuple(items)}))
    assert int(tcl.eval("llength $::ebs(files)")) == len(items)
    for i, item in enumerate(items):
        got = tcl.eval(f"binary encode hex [encoding convertto utf-8 [lindex $::ebs(files) {i}]]")
        assert got == item.encode().hex()


# R4
def test_argv_and_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    t = _expand({"top": "cpu"}, ("dc_tool", "flows/syn/run.tcl", "-x", "${params.top}"))
    assert t.argv == ("dc_tool", MAIN_FILE, "-x", "cpu")
    # The wrapper sources the params, then the user script (keeping argv0 / info script).
    tcl = _tcl()
    _write(tmp_path, t)
    script = tmp_path / "flows/syn/run.tcl"
    script.parent.mkdir(parents=True)
    script.write_text("set ::seen [list $::ebs(top) $::argv0 [info script]]\n")
    monkeypatch.chdir(tmp_path)  # Tcl resolves the relative paths against the process cwd
    tcl.eval(f"source {MAIN_FILE}")
    assert tcl.eval("set ::seen").split() == ["cpu", "flows/syn/run.tcl", "flows/syn/run.tcl"]


# R4
def test_real_tclsh(tmp_path: Path) -> None:
    tclsh = shutil.which("tclsh")
    if tclsh is None:
        pytest.skip("no `tclsh` on PATH; the quoting is still checked via tkinter.Tcl()")
    values = {
        "brace": "a}b{",
        "dollar": "$x [exit 3]",
        "bs": "c:\\dir\\",
        "nl": "l1\nl2",
        "u": "é€",
    }
    t = _expand(values, ("tclsh", "run.tcl", "arg1"))
    _write(tmp_path, t)
    (tmp_path / "run.tcl").write_text(
        "foreach k [lsort [array names ::ebs]] {\n"
        '  puts "$k=[binary encode hex [encoding convertto utf-8 $::ebs($k)]]"\n'
        "}\n"
        'puts "argv=$::argv"\n'
    )
    argv = (tclsh, *t.argv[1:])
    run = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True, check=True)
    expected = [f"{k}={values[k].encode().hex()}" for k in sorted(values)] + ["argv=arg1"]
    assert run.stdout.splitlines() == expected


# R4
@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "'command' is required"),
        ({"command": ["tclsh"]}, "tool and a script"),
        ({"script": "puts hi"}, "'script'.*command"),
        ({"command": ["tclsh", "a.tcl"], "workdir": "x"}, "'workdir'"),
    ],
)
def test_validate_errors(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(RuleError, match=message):
        TclRule().validate(step(kind="tcl", **fields))


# R5
@pytest.mark.parametrize(("exit_code", "log_tail", "expected"), CLASSIFY_CASES)
def test_classify(exit_code: int, log_tail: str, expected: Classification) -> None:
    assert TclRule().classify(exit_code, log_tail, {}) == expected


GOLDEN = {
    "version": "1",
    "argv": ["tclsh", MAIN_FILE, "-v"],
    "config_files": {
        PARAMS_FILE: (
            "# Generated by ebs (rule tcl): step params as ::ebs(name). Do not edit.\n"
            "array set ::ebs {}\n"
            "set ::ebs(lib) {core}\n"
            "set ::ebs(top) {cpu_top}\n"
        ),
        MAIN_FILE: (
            "# Generated by ebs (rule tcl): params, then the step's script. Do not edit.\n"
            "source .ebs/ebs_params.tcl\n"
            "set ::argv0 {flows/syn/run.tcl}\n"
            "source {flows/syn/run.tcl}\n"
        ),
    },
}


# R6
def test_golden_argv() -> None:
    rule = TclRule()
    t = _expand({"top": "cpu_top", "lib": "core"}, ("tclsh", "flows/syn/run.tcl", "-v"))
    actual = {"version": rule.version, "argv": list(t.argv), "config_files": dict(t.config_files)}
    assert actual == GOLDEN, (
        "the tcl rule's generated command changed: if intended, bump TclRule.version "
        "(it is part of every action key) and update GOLDEN in this test"
    )
