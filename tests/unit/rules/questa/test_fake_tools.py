"""The fake simulator against the `questa.sim` rule, hermetically (task P1-08 R5, R7): the rule's
generated script runs fake vsim in each mode, and `classify` turns the outcome into the right
classification and result.json. (The driver-level flow is tests/integration/.)"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ebs.rules.api import FAILED, INFRA, PASSED, Classification
from ebs.rules.questa.sim import QuestaSimRule
from ebs.runner.collect import log_tail
from tests.helpers.questa import FAKES_BIN, StagedResolver, qcontext
from tests.helpers.rules import step

MODES: list[tuple[str, Classification]] = [
    ("pass", PASSED),
    ("uvm_error", FAILED),
    ("fatal", FAILED),
    ("no_finish", FAILED),
    ("license_fail", INFRA("license")),
    ("segv", INFRA("tool_crash")),
]


def _sim_work(tmp_path: Path, *, libs: bool = True) -> Path:
    """A work dir as the runner stages it: config files, the model and its source library."""
    rule = QuestaSimRule()
    inputs = {"model": "${steps.elab.outputs.model}"}
    if libs:
        inputs["libs"] = "${steps.c.outputs.worklib[*]}"
    s = step(kind="questa.sim", inputs=inputs, params={"test": "t", "seed": 5})
    resolver = StagedResolver({("elab", "model"): ["elab"], ("c", "worklib"): ["c[lib=core]"]})
    t = rule.expand(s, qcontext(s, resolver))
    work = tmp_path / "work"
    for path, content in t.config_files.items():
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(content)
    (work / "model/elab/opt").mkdir(parents=True)
    (work / "model/elab/_info").write_text("lib\n")
    (work / "model/elab/opt/_primary.dat").write_text("optimized tb_top libs core\n")
    (work / "worklib/c[lib=core]").mkdir(parents=True)
    (work / "worklib/c[lib=core]/_info").write_text("lib\n")
    return work


def _run(work: Path, mode: str) -> tuple[Classification, dict[str, object]]:
    log = work.parent / "tool.log"
    with open(log, "wb") as f:
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", ".ebs/questa_sim.sh"],
            cwd=work,
            env={"PATH": f"{FAKES_BIN}:/usr/bin:/bin", "FAKE_QUESTA_MODE": mode},
            stdout=f,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=30,
        )
    result = work / "result.json"
    classification = QuestaSimRule().classify(proc.returncode, log_tail(log), {"result": result})
    doc = json.loads(result.read_text())
    assert isinstance(doc, dict)
    return classification, doc


# R5, R7
@pytest.mark.parametrize(("mode", "expected"), MODES)
def test_fake_vsim_modes_classify(tmp_path: Path, mode: str, expected: Classification) -> None:
    classification, doc = _run(_sim_work(tmp_path), mode)
    assert classification == expected
    if expected.status == "infra":
        assert doc["ebs_pending"] == "questa.sim"  # not a result: left for the retry
    else:
        assert (doc["status"], doc["test"], doc["seed"]) == (expected.status, "t", "5")


# R7: the libraries an optimized model was built from must be mapped for vsim
def test_fake_vsim_needs_the_model_libraries(tmp_path: Path) -> None:
    classification, doc = _run(_sim_work(tmp_path, libs=False), "pass")
    assert classification == FAILED
    assert "vsim-1019" in str(doc["first_error"])
