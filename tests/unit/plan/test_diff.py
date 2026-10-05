"""Tests for `diff_plans`: why each action reruns (task P0-08 R10)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.plan.diff import diff_plans
from ebs.plan.planner import Planner
from ebs.plan.types import ActionDiff, Plan
from tests.helpers.plan import (
    ARCH_STEPS,
    RTL,
    FakeQuestaRule,
    FakeSnapshotter,
    FakeToolchains,
    arch_flow,
    registry,
)

CORE = "compile[filelist=core.f,lib=core]"
ALU = "compile[filelist=alu.f,lib=alu]"
LINT_STEP = {
    "kind": "shell",
    "script": "lint ${params.mode}\n",
    "params": {"mode": "strict"},
    "env": {"LINT_LEVEL": "2"},
    "outputs": {"report": "lint.txt"},
}


def make_plan(
    cas: FsCAS,
    base: Path,
    *,
    files: dict[str, str] | None = None,
    steps: dict[str, Any] | None = None,
    salt: str | None = None,
    compile_version: str = "1",
    targets: tuple[str, ...] = (),
) -> Plan:
    toolchains = FakeToolchains()
    if salt is not None:
        toolchains.salt["questa/2025.2"] = salt
    rules = registry()
    if compile_version != "1":
        rules = type(rules)([p for p in rules.plugins() if p.kind != "questa.compile"])
        rule = FakeQuestaRule("questa.compile")
        rule.version = compile_version
        rules.register(rule)
    p = Planner(cas, FakeSnapshotter(cas, files or RTL), toolchains, rules)
    return p.plan(
        arch_flow(base, steps or {**ARCH_STEPS, "lint": LINT_STEP}), base=base, targets=targets
    )


def by_id(diffs: list[ActionDiff]) -> dict[str, ActionDiff]:
    return {d.action_id: d for d in diffs}


def _with(step: str, **changes: Any) -> dict[str, Any]:
    return {
        **ARCH_STEPS,
        "lint": LINT_STEP,
        step: {**({**ARCH_STEPS, "lint": LINT_STEP}[step]), **changes},
    }


CASES: dict[str, tuple[dict[str, Any], str, str]] = {
    "source": (
        {"files": {**RTL, "rtl/alu.sv": "module alu; wire x; endmodule\n"}},
        CORE,
        'inputs["rtl/alu.sv"]',
    ),
    "upstream": (
        {"files": {**RTL, "rtl/alu.sv": "module alu; wire x; endmodule\n"}},
        "elab",
        f'inputs["worklib/{CORE}"]',
    ),
    "param": ({"steps": _with("elab", params={"top": "tb_other"})}, "elab", "params.top"),
    "argv": ({"steps": _with("elab", params={"top": "tb_other"})}, "elab", "argv"),
    "toolchain": ({"salt": "patched"}, CORE, "toolchain"),
    "rule": ({"compile_version": "2"}, ALU, "rule"),
    "env": ({"steps": _with("lint", env={"LINT_LEVEL": "3"})}, "lint", "env.LINT_LEVEL"),
    "config_file": (
        {"steps": _with("lint", params={"mode": "lax"})},
        "lint",
        'config_files[".ebs/script.sh"]',
    ),
    "output": (
        {"steps": _with("lint", outputs={"report": "out/lint.txt"})},
        "lint",
        "outputs.report",
    ),
}


# R10
@pytest.mark.parametrize("case", sorted(CASES))
def test_changed_fields(cas: FsCAS, tmp_path: Path, case: str) -> None:
    changes, action_id, field = CASES[case]
    old = make_plan(cas, tmp_path)
    new = make_plan(cas, tmp_path, **changes)
    diff = by_id(diff_plans(old, new))[action_id]
    assert diff.status == "changed"
    assert field in diff.changed, diff.changed


# R10
def test_unchanged_added_removed(cas: FsCAS, tmp_path: Path) -> None:
    old = make_plan(cas, tmp_path, targets=("elab", "lint"))
    new = make_plan(cas, tmp_path, targets=("elab", "lint_legacy"))
    diffs = diff_plans(old, new)
    statuses = {d.action_id: d.status for d in diffs}
    assert statuses == {
        CORE: "unchanged",
        ALU: "unchanged",
        "elab": "unchanged",
        "lint": "removed",
        "lint_legacy": "added",
    }
    assert all(d.changed == () and d.pending == () for d in diffs)
    # New plan's order first, removed actions last.
    assert [d.action_id for d in diffs][-1] == "lint"


def test_only_the_changed_field_is_reported(cas: FsCAS, tmp_path: Path) -> None:
    old = make_plan(cas, tmp_path)
    new = make_plan(cas, tmp_path, files={**RTL, "rtl/core.f": "core.sv\nextra.sv\n"})
    diffs = by_id(diff_plans(old, new))
    assert diffs[CORE].changed == ('inputs["rtl/core.f"]',)
    assert diffs[ALU].status == "unchanged"


def test_added_and_removed_inputs_are_changes(cas: FsCAS, tmp_path: Path) -> None:
    old = make_plan(cas, tmp_path)
    new = make_plan(cas, tmp_path, files={**RTL, "rtl/new.sv": "module n; endmodule\n"})
    assert by_id(diff_plans(old, new))[CORE].changed == ('inputs["rtl/new.sv"]',)
    assert by_id(diff_plans(new, old))[CORE].changed == ('inputs["rtl/new.sv"]',)


# R10
def test_pending_reason(cas: FsCAS, tmp_path: Path) -> None:
    deterministic = _with("compile", outputs={"worklib": {"dir": "work/${row.lib}"}})
    old = make_plan(cas, tmp_path)
    new = make_plan(cas, tmp_path, steps=deterministic)
    diffs = by_id(diff_plans(old, new))
    assert diffs["elab"].status == "unknown"
    assert diffs["elab"].pending == (f"depends on {ALU}", f"depends on {CORE}")
    assert diffs["elab"].changed == ()  # unknown input ids are pending, not changes
    # sim waits on elab, whose key (and so its nondeterministic model id) is unknown.
    sim = next(d for d in diffs.values() if d.action_id.startswith("sim["))
    assert sim.status == "unknown"
    assert sim.pending == ("depends on elab",)
    # `deterministic` is not in the producer's key: only its consumers are affected.
    assert diffs[CORE].changed == ()
    assert diffs[CORE].status == "unchanged"


def test_unknown_old_key_is_a_change_when_new_is_known(cas: FsCAS, tmp_path: Path) -> None:
    deterministic = _with("compile", outputs={"worklib": {"dir": "work/${row.lib}"}})
    old = make_plan(cas, tmp_path, steps=deterministic)
    new = make_plan(cas, tmp_path)
    elab = by_id(diff_plans(old, new))["elab"]
    assert elab.status == "changed"
    assert elab.changed == (f'inputs["worklib/{ALU}"]', f'inputs["worklib/{CORE}"]')


def test_equal_documents_with_different_keys_report_the_schema(cas: FsCAS, tmp_path: Path) -> None:
    new = make_plan(cas, tmp_path)
    stale = dataclasses.replace(new.action("elab"), key=hash_bytes(b"key from another schema"))
    old = dataclasses.replace(
        new, actions=tuple(stale if a.action_id == "elab" else a for a in new.actions)
    )
    elab = by_id(diff_plans(old, new))["elab"]
    assert (elab.status, elab.changed) == ("changed", ("schema",))


def test_bool_int_param_change_is_named(cas: FsCAS, tmp_path: Path) -> None:
    def plan_with(value: object) -> Plan:
        step = {"kind": "shell", "command": ["x"], "params": {"n": value}}
        return make_plan(cas, tmp_path, steps={"s": step})

    diff = by_id(diff_plans(plan_with(1), plan_with(True)))["s"]
    assert (diff.status, diff.changed) == ("changed", ("params.n",))


def test_both_unknown_is_unknown(cas: FsCAS, tmp_path: Path) -> None:
    deterministic = _with("compile", outputs={"worklib": {"dir": "work/${row.lib}"}})
    plan = make_plan(cas, tmp_path, steps=deterministic)
    elab = by_id(diff_plans(plan, plan))["elab"]
    assert elab.status == "unknown"
    assert elab.pending == (f"depends on {ALU}", f"depends on {CORE}")
