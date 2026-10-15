"""`inputs.<name>.writable: true`: declared in the flow, carried to the plan, not keyed (R1)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ebs.cas.fs import FsCAS
from ebs.core.errors import PlanError
from ebs.flow.model import InputDef, StepDef
from ebs.plan.keys import compute_key
from ebs.plan.planfile import spec_from_json, spec_to_json
from ebs.plan.types import ActionSpec, Plan
from tests.helpers.plan import flow, planner

FILES = {"rtl/a.sv": "module a; endmodule\n", "rtl/b.sv": "module b; endmodule\n"}


def _plan(cas: FsCAS, base: Path, steps: dict[str, Any]) -> Plan:
    p, _ = planner(cas, FILES)
    return p.plan(flow(steps), base=base)


def _writable(spec: ActionSpec) -> dict[str, bool]:
    return {r.logical_path: r.writable for r in spec.inputs}


# R1
def test_flow_input_forms() -> None:
    step = StepDef.model_validate(
        {
            "kind": "shell",
            "script": "true",
            "inputs": {
                "a": "rtl/a.sv",
                "b": ["rtl/a.sv", "rtl/b.sv"],
                "c": {"from": "rtl/*.sv", "writable": True},
                "d": {"from": ["rtl/a.sv"]},
            },
        }
    )
    assert step.inputs["c"] == InputDef.model_validate({"from": "rtl/*.sv", "writable": True})
    d = step.inputs["d"]
    assert isinstance(d, InputDef)
    assert d.writable is False


# R1
@pytest.mark.parametrize(
    "value",
    [{"writable": True}, {"from": "a", "writable": "yes"}, {"from": "a", "copy": True}],
    ids=["no-from", "non-bool", "unknown-key"],
)
def test_flow_input_def_rejects(value: object) -> None:
    with pytest.raises(ValidationError):
        StepDef.model_validate({"kind": "shell", "script": "true", "inputs": {"x": value}})


# R1
def test_writable_source_input_planned(cas: FsCAS, tmp_path: Path) -> None:
    plan = _plan(
        cas,
        tmp_path,
        {
            "s": {
                "kind": "shell",
                "script": "true",
                "inputs": {"ro": "rtl/a.sv", "rw": {"from": ["rtl/b.sv"], "writable": True}},
            }
        },
    )
    assert _writable(plan.action("s")) == {"rtl/a.sv": False, "rtl/b.sv": True}


# R1: an output reference staged through a writable input is writable too.
def test_writable_output_ref_planned(cas: FsCAS, tmp_path: Path) -> None:
    plan = _plan(
        cas,
        tmp_path,
        {
            "p": {"kind": "shell", "script": "mkdir d", "outputs": {"d": {"dir": "d"}}},
            "c": {
                "kind": "shell",
                "script": "true",
                "inputs": {"lib": {"from": "${steps.p.outputs.d}", "writable": True}},
            },
        },
    )
    (ref,) = plan.action("c").inputs
    assert ref.writable is True


# R1: the same file staged once writable and once read-only is staged writable, in either order.
@pytest.mark.parametrize("rw_first", [False, True], ids=["ro-first", "rw-first"])
def test_writable_wins_on_overlap(cas: FsCAS, tmp_path: Path, rw_first: bool) -> None:
    ro: dict[str, Any] = {"ro": "rtl/a.sv"}
    rw: dict[str, Any] = {"rw": {"from": "rtl/a.sv", "writable": True}}
    plan = _plan(
        cas,
        tmp_path,
        {
            "s": {
                "kind": "shell",
                "script": "true",
                "inputs": {**rw, **ro} if rw_first else {**ro, **rw},
            }
        },
    )
    assert _writable(plan.action("s")) == {"rtl/a.sv": True}


# R1: writable changes how the input is staged, never what the tool computes from it.
def test_writable_not_in_key(cas: FsCAS, tmp_path: Path) -> None:
    def key(writable: bool) -> object:
        inputs = {"x": {"from": "rtl/a.sv", "writable": writable}}
        plan = _plan(cas, tmp_path, {"s": {"kind": "shell", "script": "t", "inputs": inputs}})
        return plan.action("s").key

    assert key(True) == key(False)


# R1: plan.json carries the flag; a read-only input encodes exactly as before P1-04.
def test_planfile_writable_roundtrip(cas: FsCAS, tmp_path: Path) -> None:
    plan = _plan(
        cas,
        tmp_path,
        {
            "s": {
                "kind": "shell",
                "script": "true",
                "inputs": {"ro": "rtl/a.sv", "rw": {"from": "rtl/b.sv", "writable": True}},
            }
        },
    )
    spec = plan.action("s")
    doc = json.loads(json.dumps(spec_to_json(spec)))
    ro, rw = doc["inputs"]
    assert "writable" not in ro
    assert rw["writable"] is True
    assert spec_from_json(doc) == spec
    rw["writable"] = "yes"
    with pytest.raises(PlanError, match="writable"):
        spec_from_json(doc)
    assert compute_key(dataclasses.replace(spec, inputs=tuple(
        dataclasses.replace(r, writable=False) for r in spec.inputs
    ))) == spec.key  # fmt: skip
