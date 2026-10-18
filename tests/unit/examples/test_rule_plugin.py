"""Tests for the example rule plugin in examples/rule-plugin (task D-01 R6).

The site's "Write a rule plugin" tutorial embeds this file: it uses only the public plugin API,
so a plugin author can copy it as a starting point.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import tomllib
from pathlib import Path

import pytest

from ebs.core.errors import RuleError
from ebs.flow.loader import load_flow
from ebs.flow.matrix import expand_matrix
from ebs.flow.model import StepDef
from ebs.rules.api import FAILED, INFRA, PASSED, ExpandContext, RuleSettings
from ebs.rules.registry import RuleRegistry
from ebs_verilator_lint import VerilatorLintRule, glob_word

PLUGIN = Path(__file__).resolve().parents[3] / "examples" / "rule-plugin"

FAKE_VERILATOR = """#!/bin/bash
# Records its arguments, then reports one warning per file named *_bad.sv.
printf '%s\\n' "$@" > args.txt
for f in "$@"; do
    case $f in *_bad.sv) echo "%Warning-UNUSED: $f:3:9: Signal is not used: 'spare'" >&2 ;; esac
done
"""


def lint_step(**fields: object) -> StepDef:
    base: dict[str, object] = {
        "kind": "verilator.lint",
        "inputs": {"rtl": "rtl/alu/*.sv"},
        "params": {"top": "alu"},
    }
    return StepDef.model_validate(base | fields)


def context(step: StepDef) -> ExpandContext:
    (instance,) = expand_matrix(step, {}, name="lint")
    return ExpandContext(instance)


# R6
def test_registers_like_an_installed_plugin() -> None:
    project = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))
    entry_points = project["project"]["entry-points"]["ebs.rules"]
    assert entry_points == {"verilator.lint": "ebs_verilator_lint:VerilatorLintRule"}

    class EntryPoint:
        name = "verilator.lint"
        value = "ebs_verilator_lint:VerilatorLintRule"

        def load(self) -> object:
            return VerilatorLintRule

    registry = RuleRegistry.from_entry_points(entry_points=[EntryPoint()])
    assert registry.kinds() == ("verilator.lint",)
    assert registry.get("verilator.lint").version == "1"


# R6
def test_expand_generates_script_and_log_output() -> None:
    step = lint_step(params={"top": "alu", "opts": "-Wno-DECLFILENAME --x-assign 0"})
    template = VerilatorLintRule().expand(step, context(step))
    assert template.argv == ("bash", "--noprofile", "--norc", ".ebs/verilator_lint.sh")
    script = template.config_files[".ebs/verilator_lint.sh"]
    assert "files=(rtl/alu/*.sv)" in script  # the glob is expanded by bash in the work dir
    assert "--top-module alu" in script
    assert "-Wno-DECLFILENAME --x-assign 0" in script
    assert "-Wno-fatal" in script
    assert template.outputs["log"].file == "lint.log"


# R6
@pytest.mark.parametrize(
    ("pattern", "word"),
    [
        ("rtl/alu/*.sv", "rtl/alu/*.sv"),
        ("my rtl/*.sv", "'my rtl/'*.sv"),
        ("a$(x)/[ab]?.v", "'a$(x)/'[ab]?.v"),
        ("plain.sv", "plain.sv"),
    ],
)
def test_glob_word(pattern: str, word: str) -> None:
    assert glob_word(pattern) == word


# R6
def test_fatal_warnings_param() -> None:
    step = lint_step(params={"top": "alu", "fatal_warnings": True})
    script = VerilatorLintRule().expand(step, context(step)).config_files[".ebs/verilator_lint.sh"]
    assert "-Wno-fatal" not in script


# R6
@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"params": {}}, "'top' param is required"),
        ({"params": {"top": "alu", "speed": "1"}}, "unknown param 'speed'"),
        ({"inputs": {}}, "needs an 'rtl' input"),
        ({"script": "echo"}, "does not take 'script'"),
    ],
)
def test_validate_rejects(fields: dict[str, object], message: str) -> None:
    with pytest.raises(RuleError, match=message):
        VerilatorLintRule().validate(lint_step(**fields))


# R6
def test_generated_script_runs(tmp_path: Path) -> None:
    step = lint_step(inputs={"rtl": "rtl/**/*.sv"})
    template = VerilatorLintRule().expand(step, context(step))
    for rel, content in template.config_files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content)
    for rel in ("rtl/alu/alu.sv", "rtl/alu/alu_bad.sv", "rtl/x y/z.sv"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("module m; endmodule\n")
    tool = tmp_path / "bin" / "verilator"
    tool.parent.mkdir()
    tool.write_text(FAKE_VERILATOR)
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    env = {"PATH": f"{tool.parent}:{os.environ['PATH']}"}
    run = subprocess.run(template.argv, cwd=tmp_path, env=env, capture_output=True, check=False)
    assert run.returncode == 0, run.stderr
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[-3:] == ["rtl/alu/alu.sv", "rtl/alu/alu_bad.sv", "rtl/x y/z.sv"]
    log = tmp_path / "lint.log"
    assert VerilatorLintRule().summarize({"log": log}, log) == {"warnings": 1, "errors": 0}


# R6
@pytest.mark.parametrize(
    ("exit_code", "log_tail", "expected"),
    [
        (0, "%Warning-UNUSED: a.sv:1:1: x\n", PASSED),
        (1, "%Error: a.sv:3:1: syntax error\n", FAILED),
        (127, "bash: verilator: command not found\n", INFRA("tool_missing")),
        (-signal.SIGSEGV, "", INFRA("tool_crash")),
        (128 + signal.SIGSEGV, "%Error: a.sv:3:1: x\n", INFRA("tool_crash")),  # crash wins
        (1, "license checkout failed\n", INFRA("license")),
    ],
)
def test_classify(exit_code: int, log_tail: str, expected: object) -> None:
    rule = VerilatorLintRule(RuleSettings())
    assert rule.classify(exit_code, log_tail, {}) == expected


def test_example_flow_loads() -> None:
    flow = load_flow(PLUGIN / "flow.yaml")
    assert flow.steps["lint"].kind == "verilator.lint"


def test_summarize_without_log(tmp_path: Path) -> None:
    missing = tmp_path / "lint.log"
    assert VerilatorLintRule().summarize({}, missing) == {"warnings": 0, "errors": 0}
