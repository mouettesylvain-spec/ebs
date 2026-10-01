"""Tests for the `shell` rule (task P0-12 R2, R5, R6)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ebs.core.errors import FlowError, RuleError
from ebs.flow.interp import Ref, SourceLocation
from ebs.flow.matrix import expand_matrix
from ebs.flow.model import StepDef
from ebs.rules.api import Classification, ExpandContext
from ebs.rules.shell import ShellRule
from tests.helpers.rules import CLASSIFY_CASES, context, step

BASH = ("bash", "--noprofile", "--norc", "-eo", "pipefail")


# R2
def test_script_is_config_file(tmp_path: Path) -> None:
    s = step(
        kind="shell", script="echo ${params.msg} > out.txt\nfalse | true\n", params={"msg": "hi"}
    )
    t = ShellRule().expand(s, context(s))
    assert t.argv == (*BASH, ".ebs/script.sh")
    assert dict(t.config_files) == {".ebs/script.sh": "echo hi > out.txt\nfalse | true\n"}
    # The script content lives only in the config file (hashed), never in argv.
    assert not any("echo" in a for a in t.argv)

    # The generated argv runs the file with -e and pipefail.
    (tmp_path / ".ebs").mkdir()
    (tmp_path / ".ebs/script.sh").write_text("false | true\necho after\n")
    run = subprocess.run(t.argv, cwd=tmp_path, capture_output=True, text=True, check=False)
    assert run.returncode == 1
    assert "after" not in run.stdout


# R2
def test_command_argv() -> None:
    s = step(
        kind="shell",
        command=["python3", "gen.py", "--top", "${params.top}", "$${HOME}"],
        params={"top": "cpu"},
        env={"FOO": "bar"},
    )
    t = ShellRule().expand(s, context(s))
    assert t.argv == ("python3", "gen.py", "--top", "cpu", "${HOME}")
    assert dict(t.config_files) == {}
    assert dict(t.env) == {"FOO": "bar"}
    assert dict(t.runtime_env) == {}


# R2
def test_command_splices_list_refs() -> None:
    class Outputs:
        def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
            return ["a/x.v", "b/x.v"]

    s = step(kind="shell", command=["cat", "${steps.gen.outputs.v[*]}"])
    (inst,) = expand_matrix(s, {}, name="s")
    t = ShellRule().expand(s, ExpandContext(inst, Outputs()))
    assert t.argv == ("cat", "a/x.v", "b/x.v")

    unresolved = step(kind="shell", command=["cat", "${steps.gen.outputs.v[*]}"])
    with pytest.raises(FlowError):
        ShellRule().expand(unresolved, context(unresolved))


# R2
def test_exactly_one() -> None:
    rule = ShellRule()
    with pytest.raises(RuleError, match=r"exactly one of 'script' or 'command'.*found: none"):
        rule.validate(step(kind="shell"))
    both = StepDef.model_construct(kind="shell", script="true", command=("true",))
    with pytest.raises(RuleError, match="exactly one of 'script' or 'command'"):
        rule.validate(both)
    with pytest.raises(RuleError, match="exactly one"):
        rule.expand(step(kind="shell"), context(step(kind="shell")))
    with pytest.raises(RuleError, match="must not be empty"):
        rule.validate(step(kind="shell", command=[]))
    with pytest.raises(RuleError, match=r"'workdir'.*make"):
        rule.validate(step(kind="shell", script="true", workdir="x"))
    rule.validate(step(kind="shell", script="true"))
    rule.validate(step(kind="shell", command=["true"]))


# R2
def test_script_must_render_to_text() -> None:
    class Outputs:
        def resolve(self, ref: Ref, *, where: SourceLocation) -> str | list[str]:
            return ["a", "b"]

    s = step(kind="shell", script="${steps.gen.outputs.v[*]}")
    (inst,) = expand_matrix(s, {}, name="s")
    with pytest.raises(RuleError, match=r"script.*list"):
        ShellRule().expand(s, ExpandContext(inst, Outputs()))


# R5
@pytest.mark.parametrize(("exit_code", "log_tail", "expected"), CLASSIFY_CASES)
def test_classify(exit_code: int, log_tail: str, expected: Classification) -> None:
    assert ShellRule().classify(exit_code, log_tail, {}) == expected


# R6: argv and generated files for a reference step are fixed; a change needs a version bump.
GOLDEN = {
    "version": "1",
    "argv": [*BASH, ".ebs/script.sh"],
    "config_files": {".ebs/script.sh": "make -C rtl lint 2>&1 | tee lint.log\n"},
    "command_argv": ["verilator", "--lint-only", "top.sv"],
}


# R6
def test_golden_argv() -> None:
    rule = ShellRule()
    s = step(kind="shell", script="make -C rtl lint 2>&1 | tee lint.log\n")
    t = rule.expand(s, context(s))
    c = step(kind="shell", command=["verilator", "--lint-only", "top.sv"])
    actual = {
        "version": rule.version,
        "argv": list(t.argv),
        "config_files": dict(t.config_files),
        "command_argv": list(rule.expand(c, context(c)).argv),
    }
    assert actual == GOLDEN, (
        "the shell rule's generated command changed: if intended, bump ShellRule.version "
        "(it is part of every action key) and update GOLDEN in this test"
    )
