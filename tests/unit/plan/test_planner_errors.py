"""Planner error paths: every message names the step/action and says what to change (P0-08)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.core.errors import PlanError
from ebs.flow.model import OutputDef, StepDef
from ebs.plan.planner import Planner
from ebs.rules.api import ActionTemplate, BaseRule, ExpandContext
from tests.helpers.plan import (
    ARCH_STEPS,
    RTL,
    FakeSnapshotter,
    FakeToolchains,
    arch_flow,
    flow,
    planner,
    registry,
    write_tables,
)


class GeneratingRule(BaseRule):
    """A rule with an implicit output `log` and an implicit input `ebs.extra`."""

    kind = "gen"
    version = "1"

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        return ActionTemplate(
            argv=("gen",),
            inputs={"ebs.extra": "rtl/core.f"},
            outputs={"log": OutputDef(file="gen.log")},
        )


class ClashingRule(GeneratingRule):
    """Generates an input named like a declared one (rules should use `ebs.` names)."""

    kind = "clash"

    def expand(self, step: StepDef, ctx: ExpandContext) -> ActionTemplate:
        return ActionTemplate(argv=("clash",), inputs={"srcs": "rtl/core.f"})


def plan_steps(cas: FsCAS, base: Path, steps: dict[str, Any], **top: Any) -> None:
    rules = registry()
    rules.register(GeneratingRule())
    rules.register(ClashingRule())
    Planner(cas, FakeSnapshotter(cas, RTL), FakeToolchains(), rules).plan(
        flow(steps, **top), base=base
    )


def test_generated_outputs_are_referenceable(cas: FsCAS, tmp_path: Path) -> None:
    rules = registry()
    rules.register(GeneratingRule())
    steps: dict[str, Any] = {
        "g": {"kind": "gen"},
        "use": {"kind": "shell", "command": ["cat", "${steps.g.outputs.log}"]},
    }
    plan = Planner(cas, FakeSnapshotter(cas, RTL), FakeToolchains(), rules).plan(
        flow(steps), base=tmp_path
    )
    gen = plan.action("g")
    assert [o.name for o in gen.outputs] == ["log"]
    assert [i.logical_path for i in gen.inputs] == ["rtl/core.f"]
    assert plan.action("use").argv == ("cat", "log/g/gen.log")


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        (
            {"g": {"kind": "gen", "outputs": {"log": "mine.log"}}},
            r"steps\.g: output 'log' is declared in the flow and also generated",
        ),
        (
            {"c": {"kind": "clash", "inputs": {"srcs": "rtl/**"}}},
            r"steps\.c: input 'srcs' is declared in the flow and also generated",
        ),
        (
            {"m": {"kind": "make", "workdir": "flows/lint", "target": "-k"}},
            r"steps\.m: step 'm': make target '-k'",
        ),
        (
            {
                "s": {"kind": "shell", "command": ["x"], "inputs": {"a": "${steps.t.outputs.o}/x"}},
                "t": {"kind": "shell", "command": ["y"], "outputs": {"o": {"dir": "d"}}},
            },
            r"steps\.s: inputs\.a .*must be the whole input value",
        ),
        (
            {
                "s": {
                    "kind": "shell",
                    "command": ["x"],
                    "params": {"p": "${steps.t.outputs.o[*]}"},
                    "outputs": {"o": "${params.p}"},
                },
                "t": {"kind": "shell", "command": ["y"], "outputs": {"o": "o.txt"}},
            },
            r"steps\.s: outputs\.o .* expands to a list",
        ),
        (
            {
                "s": {
                    "kind": "shell",
                    "command": ["x"],
                    "params": {"m": "lots"},
                    "debug": {"collect": ["*.log"], "max_size": "${params.m}"},
                }
            },
            r"steps\.s: debug\.max_size: invalid memory 'lots'",
        ),
    ],
)
def test_step_errors(cas: FsCAS, tmp_path: Path, steps: dict[str, Any], message: str) -> None:
    with pytest.raises(PlanError, match=message):
        plan_steps(cas, tmp_path, steps)


def test_deferred_debug_max_size_is_parsed(cas: FsCAS, tmp_path: Path) -> None:
    write_tables(tmp_path, {"t.csv": "n,size\na,1G\nb,2M\n"})
    step = {
        "kind": "shell",
        "command": ["x"],
        "matrix": {"table": "t.csv"},
        "debug": {"collect": ["${row.n}.log"], "max_size": "${row.size}"},
    }
    p, _ = planner(cas, RTL)
    a, b = p.plan(flow({"s": step}), base=tmp_path).actions
    assert (a.debug.collect, a.debug.max_size) == (("a.log",), 1 << 30)
    assert (b.debug.collect, b.debug.max_size) == (("b.log",), 2 << 20)


def test_toolchain_default_resources_must_be_literal(cas: FsCAS, tmp_path: Path) -> None:
    toolchains = {"questa": {"module": "questa/2025.2", "resources": {"time": "${row.t}"}}}
    step = {"kind": "shell", "toolchain": "questa", "command": ["x"]}
    with pytest.raises(PlanError, match=r"toolchain default resources\.time .* must be a literal"):
        plan_steps(cas, tmp_path, {"s": step}, toolchains=toolchains)


def test_refine_rejects_unknown_outputs(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path, ARCH_STEPS), base=tmp_path)
    with pytest.raises(PlanError, match=r"action 'elab' has no output 'modle'"):
        p.refine(plan, {("elab", "modle"): hash_bytes(b"x")})
