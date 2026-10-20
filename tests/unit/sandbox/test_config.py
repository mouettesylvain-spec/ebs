"""Per-flow and per-step sandbox choice (task P3-01 R4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ebs.cas.fs import FsCAS
from ebs.plan.planfile import decode, encode
from ebs.plan.types import Plan
from tests.helpers.plan import ARCH_STEPS, RTL, arch_flow, flow, planner


def _plan(cas: FsCAS, base: Path, steps: dict[str, Any], **top: Any) -> Plan:
    p, _ = planner(cas, RTL)
    flow_ = arch_flow(base, steps)
    if top:
        flow_ = flow_.model_copy(update=top)
    return p.plan(flow_, base=base)


def _modes(plan: Plan) -> dict[str, set[str]]:
    modes: dict[str, set[str]] = {}
    for spec in plan.actions:
        modes.setdefault(spec.step, set()).add(spec.sandbox)
    return modes


# R4: default `auto` for Questa kinds, `staging` for everything else.
def test_defaults(cas: FsCAS, tmp_path: Path) -> None:
    modes = _modes(_plan(cas, tmp_path, ARCH_STEPS))
    assert modes == {
        "compile": {"auto"},
        "elab": {"auto"},
        "sim": {"auto"},
        "lint_legacy": {"staging"},
    }


# R4: a flow-level setting opts every step in or out; a step setting wins over it.
@pytest.mark.parametrize(
    ("flow_mode", "sim_mode", "expected_sim", "expected_lint"),
    [
        ("staging", None, "staging", "staging"),  # opt out the whole flow
        ("bwrap", None, "bwrap", "bwrap"),  # opt in the whole flow
        ("staging", "bwrap", "bwrap", "staging"),  # step opts in
        (None, "staging", "staging", "staging"),  # step opts out of the Questa default
        ("bwrap", "auto", "auto", "bwrap"),
    ],
)
def test_opt_in_out(
    cas: FsCAS,
    tmp_path: Path,
    flow_mode: str | None,
    sim_mode: str | None,
    expected_sim: str,
    expected_lint: str,
) -> None:
    steps = {**ARCH_STEPS}
    if sim_mode is not None:
        steps["sim"] = {**steps["sim"], "sandbox": sim_mode}
    top = {} if flow_mode is None else {"sandbox": flow_mode}
    plan = _plan(cas, tmp_path, steps, **top)
    modes = _modes(plan)
    assert modes["sim"] == {expected_sim}
    assert modes["lint_legacy"] == {expected_lint}
    # The choice survives plan.json, so the runner sees it.
    assert _modes(decode(encode(plan))) == modes


# R4
@pytest.mark.parametrize("where", ["flow", "step"])
def test_unknown_mode_rejected(where: str) -> None:
    step = {"kind": "shell", "script": "true"}
    steps, top = (
        ({"s": step}, {"sandbox": "docker"})
        if where == "flow"
        else ({"s": {**step, "sandbox": "docker"}}, {})
    )
    with pytest.raises(ValidationError, match="sandbox"):
        flow(steps, **top)


# R4: plans that do not use a sandbox encode exactly as before (no `sandbox` member).
def test_staging_not_encoded(cas: FsCAS, tmp_path: Path) -> None:
    steps = {"lint_legacy": ARCH_STEPS["lint_legacy"]}
    assert b'"sandbox"' not in encode(_plan(cas, tmp_path, steps))
