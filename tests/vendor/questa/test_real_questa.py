"""The Questa rule pack against the real tools (task P1-08 R8; nightly, on nodes with Questa
and its licenses: `make nightly` / the vendor job).

A tiny UVM-free SystemVerilog bench is compiled, optimized and simulated through the rules'
generated scripts and config files, staged the way the runner stages them; a deliberately
failing run must be FAILED with the expected first-error signature. The environment is the
caller's: load the Questa module (and license settings) before running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ebs.flow.model import StepDef
from ebs.rules.api import PASSED, ActionTemplate, Classification, RulePlugin
from ebs.rules.questa.compile import QuestaCompileRule
from ebs.rules.questa.opt import QuestaOptRule
from ebs.rules.questa.sim import QuestaSimRule
from ebs.runner.collect import log_tail
from tests.helpers.questa import StagedResolver, qcontext, qcontexts
from tests.helpers.rules import step

COUNTER = """\
module counter(input logic clk, rst, output logic [3:0] count);
  always_ff @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;
endmodule
"""
TB = """\
module tb_top;
  logic clk = 0, rst = 1;
  logic [3:0] count;
  int expected = 0;
  counter dut(.clk, .rst, .count);
  always #5 clk = ~clk;
  initial begin
    @(negedge clk) rst = 0;
    repeat (8) begin
      @(negedge clk) expected++;
      if ($test$plusargs("break")) expected++;
      if (count != expected) $fatal(1, "count mismatch: got %0d expected %0d", count, expected);
    end
    $display("counter test passed");
    $finish;
  end
endmodule
"""


@pytest.fixture(autouse=True)
def _questa() -> None:
    missing = [tool for tool in ("vlib", "vlog", "vopt", "vsim") if shutil.which(tool) is None]
    if missing:
        pytest.skip(
            f"missing dependency: Questa ({', '.join(missing)} not on PATH; load its module)"
        )


def _run(rule: RulePlugin, t: ActionTemplate, work: Path) -> tuple[Classification, Path]:
    for path, content in t.config_files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    log = work / "tool.log"
    with open(log, "wb") as f:
        proc = subprocess.run(
            list(t.argv),
            cwd=work,
            env={**os.environ, **t.env},
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
        )
    outputs = {name: work / (out.dir or out.file or "") for name, out in t.outputs.items()}
    present = {name: path for name, path in outputs.items() if path.exists()}
    return rule.classify(proc.returncode, log_tail(log), present), log


def _stage(src: Path, work: Path, staged: str) -> None:
    shutil.copytree(src, work / staged, symlinks=True)


def _flow(tmp_path: Path, plusargs: str) -> tuple[Classification, Path, str]:
    """compile (lib rtl) -> compile (lib tb) -> opt -> sim; returns sim's result and log."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "counter.sv").write_text(COUNTER)
    (src / "tb_top.sv").write_text(TB)

    # Matrixed compile steps, so staged library paths carry `[lib=…]` as in real flows:
    # rtl is `c[lib=rtl]`, tb is `t[lib=tb]` and imports nothing but needs rtl for `counter`.
    staged = {"rtl": "worklib/c[lib=rtl]", "tb": "worklib/t[lib=tb]"}

    def compile_lib(step_name: str, lib: str, file: str, deps: list[str]) -> None:
        work = tmp_path / f"compile_{lib}"
        work.mkdir()
        shutil.copy(src / file, work / file)
        inputs: dict[str, object] = {"files": ["${row.file}"]}
        if deps:
            inputs["deps"] = "${steps.c.outputs.worklib[*]}"
            for dep in deps:
                _stage(tmp_path / f"compile_{dep}" / "work" / dep, work, staged[dep])
        s: StepDef = step(
            kind="questa.compile",
            matrix={"table": "t.csv", "id": ["lib"]},
            inputs=inputs,
            params={"lib": "${row.lib}"},
        )
        resolver = StagedResolver({("c", "worklib"): [staged[d].split("/", 1)[1] for d in deps]})
        (ctx,) = qcontexts(s, [{"lib": lib, "file": file}], name=step_name, resolver=resolver)
        rule = QuestaCompileRule()
        classification, log = _run(rule, rule.expand(s, ctx), work)
        assert classification == PASSED, log.read_text()

    compile_lib("c", "rtl", "counter.sv", [])
    compile_lib("t", "tb", "tb_top.sv", ["rtl"])
    producers = [staged[lib].split("/", 1)[1] for lib in ("rtl", "tb")]

    opt_work = tmp_path / "elab"
    opt_work.mkdir()
    for lib in ("rtl", "tb"):
        _stage(tmp_path / f"compile_{lib}" / "work" / lib, opt_work, staged[lib])
    opt = QuestaOptRule()
    s = step(
        kind="questa.opt",
        inputs={"libs": "${steps.c.outputs.worklib[*]}"},
        params={"top": "tb_top"},
    )
    t = opt.expand(s, qcontext(s, StagedResolver({("c", "worklib"): producers})))
    classification, log = _run(opt, t, opt_work)
    assert classification == PASSED, log.read_text()

    sim_work = tmp_path / "sim"
    sim_work.mkdir()
    _stage(opt_work / "opt", sim_work, "model/elab")
    for lib in ("rtl", "tb"):  # the optimized design refers to its source libraries
        _stage(tmp_path / f"compile_{lib}" / "work" / lib, sim_work, staged[lib])
    sim = QuestaSimRule()
    s = step(
        kind="questa.sim",
        inputs={"model": "${steps.elab.outputs.model}", "libs": "${steps.c.outputs.worklib[*]}"},
        params={"seed": 1, "plusargs": plusargs},
    )
    resolver = StagedResolver({("elab", "model"): ["elab"], ("c", "worklib"): producers})
    classification, log = _run(sim, sim.expand(s, qcontext(s, resolver)), sim_work)
    return classification, sim_work / "result.json", log.read_text()


# R8
def test_counter_passes(tmp_path: Path) -> None:
    classification, result, log = _flow(tmp_path, "")
    assert classification == PASSED, log
    doc = json.loads(result.read_text())
    assert (doc["status"], doc["finished"], doc["first_error"]) == ("passed", True, None)


# R8
def test_broken_counter_fails_with_signature(tmp_path: Path) -> None:
    classification, result, log = _flow(tmp_path, "+break")
    assert classification.status == "failed", log
    doc = json.loads(result.read_text())
    assert doc["fatals"] >= 1, log
    assert doc["signature"].startswith("** Fatal: count mismatch: got <n> expected <n>"), log
