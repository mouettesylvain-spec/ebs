"""Invariant I14: nondeterministic outputs pass the producer's key downstream (P0-08 R6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ebs.cas.fs import FsCAS
from ebs.core.digest import hash_bytes
from ebs.plan.keys import nondeterministic_output_id
from tests.helpers.plan import ARCH_STEPS, RTL, arch_flow, planner

CORE = "compile[filelist=core.f,lib=core]"
ALU = "compile[filelist=alu.f,lib=alu]"


# R6 (I14)
def test_nd_id_stable_across_producer_runs(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path), base=tmp_path)
    core_key = plan.action(CORE).key
    assert core_key is not None
    elab = plan.action("elab")
    assert elab.input(f"worklib/{CORE}").id == nondeterministic_output_id(core_key, "worklib")
    assert elab.key is not None  # known before any compile ran

    # Two runs of the producers yield different bytes (Questa timestamps): keys do not move.
    run1 = {
        (CORE, "worklib"): hash_bytes(b"run 1 core"),
        (ALU, "worklib"): hash_bytes(b"run 1 alu"),
    }
    run2 = {
        (CORE, "worklib"): hash_bytes(b"run 2 core"),
        (ALU, "worklib"): hash_bytes(b"run 2 alu"),
    }
    after1, after2 = p.refine(plan, run1), p.refine(plan, run2)
    assert after1 == after2 == plan
    # Re-planning reproduces the same downstream keys.
    again = p.plan(arch_flow(tmp_path), base=tmp_path)
    assert [a.key for a in again.actions] == [a.key for a in plan.actions]


# R6 (I14)
def test_deterministic_pending(cas: FsCAS, tmp_path: Path) -> None:
    steps: dict[str, Any] = dict(ARCH_STEPS)
    steps["compile"] = {**ARCH_STEPS["compile"], "outputs": {"worklib": {"dir": "work/${row.lib}"}}}
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path, steps), base=tmp_path)
    elab = plan.action("elab")
    assert [i.id for i in elab.inputs] == [None, None]
    assert elab.key is None
    # Transitively: elab's own nondeterministic output has no id while elab's key is unknown.
    for sim in (a for a in plan.actions if a.step == "sim"):
        assert sim.input("model/elab").id is None
        assert sim.key is None

    # Early cutoff: the consumer key follows the producer's content digest, not its key.
    same = {(CORE, "worklib"): hash_bytes(b"c"), (ALU, "worklib"): hash_bytes(b"a")}
    other = {**same, (ALU, "worklib"): hash_bytes(b"a2")}
    k1 = p.refine(plan, same).action("elab").key
    assert k1 is not None
    assert p.refine(plan, dict(same)).action("elab").key == k1
    assert p.refine(plan, other).action("elab").key not in {None, k1}


# R6 (I14)
def test_nd_id_never_from_produced(cas: FsCAS, tmp_path: Path) -> None:
    steps: dict[str, Any] = dict(ARCH_STEPS)
    steps["compile"] = {**ARCH_STEPS["compile"], "outputs": {"worklib": {"dir": "work/${row.lib}"}}}
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path, steps), base=tmp_path)
    model_bytes = {("elab", "model"): hash_bytes(b"model bytes")}
    early = p.refine(plan, model_bytes)  # elab's key is still unknown
    for sim in (a for a in early.actions if a.step == "sim"):
        assert sim.input("model/elab").id is None
        assert sim.key is None
    done = p.refine(
        plan,
        {**model_bytes, (CORE, "worklib"): hash_bytes(b"c"), (ALU, "worklib"): hash_bytes(b"a")},
    )
    elab_key = done.action("elab").key
    assert elab_key is not None
    for sim in (a for a in done.actions if a.step == "sim"):
        assert sim.input("model/elab").id == nondeterministic_output_id(elab_key, "model")
