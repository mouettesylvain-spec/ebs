"""Tests for the planner: expansion, references, targets, sandbox paths, refine (P0-08)."""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.core.errors import PlanError
from ebs.plan.keys import compute_key, nondeterministic_output_id
from ebs.plan.planner import Planner, Snapshotter
from ebs.plan.types import (
    ActionOutputInput,
    ActionSpec,
    DebugSpec,
    InputRef,
    OutputSpec,
    Plan,
    PlanToolchain,
    SourceInput,
)
from ebs.sources.snapshot import SnapshotResult, SourceSnapshotter
from ebs.sources.statcache import StatCache
from tests.helpers.plan import (
    ARCH_STEPS,
    RTL,
    TOOLS_ROOT,
    FakeSnapshotter,
    FakeToolchains,
    arch_flow,
    flow,
    planner,
    registry,
    write_tables,
)

CORE = "compile[filelist=core.f,lib=core]"
ALU = "compile[filelist=alu.f,lib=alu]"


def steps_of(plan: Plan, step: str) -> list[ActionSpec]:
    return [a for a in plan.actions if a.step == step]


def arch_plan(cas: FsCAS, base: Path, steps: dict[str, Any] | None = None, **kw: Any) -> Plan:
    p, _ = planner(cas, RTL)
    return p.plan(arch_flow(base, steps), base=base, **kw)


def with_step(name: str, step: dict[str, Any]) -> dict[str, Any]:
    return {**ARCH_STEPS, name: step}


# R1
def test_matrix_expansion(cas: FsCAS, tmp_path: Path) -> None:
    plan = arch_plan(cas, tmp_path)
    compiles = steps_of(plan, "compile")
    assert [a.action_id for a in compiles] == [CORE, ALU]
    core = compiles[0]
    assert core.params == {"lib": "core", "vlog_opts": "-sv -timescale 1ns/1ps"}
    assert core.argv == ("compile", "lib=core", "vlog_opts=-sv -timescale 1ns/1ps")
    assert core.outputs == (OutputSpec("worklib", "work/core", "dir", deterministic=False),)
    assert core.inputs == (
        InputRef(
            "rtl/alu.sv",
            "file",
            SourceInput("srcs", "rtl/**/*.sv"),
            hash_bytes(RTL["rtl/alu.sv"].encode()),
        ),
        InputRef(
            "rtl/core.f",
            "file",
            SourceInput("filelist", "rtl/core.f"),
            hash_bytes(RTL["rtl/core.f"].encode()),
        ),
        InputRef(
            "rtl/core.sv",
            "file",
            SourceInput("srcs", "rtl/**/*.sv"),
            hash_bytes(RTL["rtl/core.sv"].encode()),
        ),
    )
    assert core.toolchain is not None
    assert (core.toolchain.name, core.toolchain.module) == ("questa", "questa/2025.2")
    assert core.licenses == {"msimhdlsim": 1}
    assert (core.resources.cpus, core.resources.mem, core.resources.time) == (2, 8 << 30, 1800)
    assert core.domain == "cpu-nda"
    assert core.key == compute_key(core)
    sims = steps_of(plan, "sim")
    assert len(sims) == 3
    assert sims[2].params == {"test": "alu_rand", "seed": "17", "plusargs": "+a=1"}
    assert sims[2].resources.time == 3600
    assert sims[0].debug == DebugSpec(("transcript", "*.log"), 2 << 30, False)
    assert {o.name: o.optional for o in sims[0].outputs} == {"cov": True, "result": False}


# R1
def test_fan_in_star(cas: FsCAS, tmp_path: Path) -> None:
    plan = arch_plan(cas, tmp_path)
    core, alu = steps_of(plan, "compile")
    (elab,) = steps_of(plan, "elab")
    assert core.key is not None
    assert alu.key is not None
    assert elab.inputs == (
        InputRef(
            f"worklib/{ALU}",
            "tree",
            ActionOutputInput(ALU, "worklib"),
            nondeterministic_output_id(alu.key, "worklib"),
        ),
        InputRef(
            f"worklib/{CORE}",
            "tree",
            ActionOutputInput(CORE, "worklib"),
            nondeterministic_output_id(core.key, "worklib"),
        ),
    )
    assert (CORE, "worklib", "elab") in plan.edges
    assert (ALU, "worklib", "elab") in plan.edges
    assert elab.key is not None
    # Producers come before consumers.
    order = [a.action_id for a in plan.actions]
    assert order.index(CORE) < order.index("elab") < order.index(steps_of(plan, "sim")[0].action_id)
    # A single-instance reference stages the tree at <output>/<instance id>.
    sim = steps_of(plan, "sim")[0]
    assert sim.inputs == (
        InputRef(
            "model/elab",
            "tree",
            ActionOutputInput("elab", "model"),
            nondeterministic_output_id(elab.key, "model"),
        ),
    )


# R1
def test_file_output_fan_in_keeps_the_file_name(cas: FsCAS, tmp_path: Path) -> None:
    report = {
        "kind": "shell",
        "command": ["merge", "${steps.sim.outputs.result[*]}"],
        "outputs": {"merged": "merged.json"},
    }
    plan = arch_plan(cas, tmp_path, with_step("report", report))
    (action,) = steps_of(plan, "report")
    sim_ids = [a.action_id for a in steps_of(plan, "sim")]
    expected = sorted(f"result/{sid}/result.json" for sid in sim_ids)
    assert [i.logical_path for i in action.inputs] == expected
    assert all(i.kind == "file" and i.id is None for i in action.inputs)  # deterministic: pending
    assert action.argv == ("merge", *(f"result/{sid}/result.json" for sid in sim_ids))
    assert action.key is None


# R1
def test_selector(cas: FsCAS, tmp_path: Path) -> None:
    pick = {
        "kind": "questa.opt",
        "command": ["-L", "${steps.compile.outputs.worklib[lib=core]}"],
        "outputs": {"o": "o.txt"},
    }
    plan = arch_plan(cas, tmp_path, with_step("pick", pick))
    (action,) = steps_of(plan, "pick")
    assert action.argv == ("opt", "-L", f"worklib/{CORE}")
    assert [i.logical_path for i in action.inputs] == [f"worklib/{CORE}"]
    assert (CORE, "worklib", "pick") in plan.edges
    assert (ALU, "worklib", "pick") not in plan.edges


# R1
def test_selector_without_match_lists_instances(cas: FsCAS, tmp_path: Path) -> None:
    pick = {"kind": "shell", "command": ["x", "${steps.compile.outputs.worklib[lib=nope]}"]}
    with pytest.raises(PlanError, match=r"lib=nope.*compile\[filelist=core\.f,lib=core\]"):
        arch_plan(cas, tmp_path, with_step("pick", pick))


# R1
def test_matrix_ref_without_selector_error(cas: FsCAS, tmp_path: Path) -> None:
    pick = {"kind": "shell", "inputs": {"w": "${steps.compile.outputs.worklib}"}, "command": ["x"]}
    with pytest.raises(PlanError, match=r"compile.*matrix.*\[\*\]"):
        arch_plan(cas, tmp_path, with_step("pick", pick))


# R2
@pytest.mark.parametrize(
    ("ref", "suggestion"),
    [
        ("${steps.compil.outputs.worklib[*]}", "did you mean 'compile'"),
        ("${steps.compile.outputs.worklb[*]}", "did you mean 'worklib'"),
    ],
)
def test_unknown_ref_suggestion(cas: FsCAS, tmp_path: Path, ref: str, suggestion: str) -> None:
    pick = {"kind": "shell", "command": ["x", ref]}
    with pytest.raises(PlanError, match=suggestion) as exc:
        arch_plan(cas, tmp_path, with_step("pick", pick))
    assert "steps.pick" in str(exc.value)


# R2
def test_step_cycle_reported_with_path(cas: FsCAS, tmp_path: Path) -> None:
    steps = {
        "a": {"kind": "shell", "command": ["x", "${steps.c.outputs.o}"], "outputs": {"o": "a"}},
        "b": {"kind": "shell", "command": ["x", "${steps.a.outputs.o}"], "outputs": {"o": "b"}},
        "c": {"kind": "shell", "command": ["x", "${steps.b.outputs.o}"], "outputs": {"o": "c"}},
    }
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"a -> b -> c -> a"):
        p.plan(flow(steps), base=tmp_path)


# R3
def test_targets_closure(cas: FsCAS, tmp_path: Path) -> None:
    toolchains = FakeToolchains()
    p = Planner(cas, FakeSnapshotter(cas, RTL), toolchains, registry())
    plan = p.plan(arch_flow(tmp_path), base=tmp_path, targets=["elab"])
    assert {a.step for a in plan.actions} == {"compile", "elab"}
    assert {name for name, _ in toolchains.calls} == {"questa"}  # spyglass never resolved
    assert set(plan.toolchains) == {"questa"}
    plan = p.plan(arch_flow(tmp_path), base=tmp_path, targets=["sim", "lint_legacy"])
    assert {a.step for a in plan.actions} == {"compile", "elab", "sim", "lint_legacy"}


# R3
def test_unknown_target_suggestion(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"unknown target 'sims'.*did you mean 'sim'"):
        p.plan(arch_flow(tmp_path), base=tmp_path, targets=["sims"])


BAD_PATHS: dict[str, dict[str, Any]] = {
    "argv_home": {"kind": "shell", "command": ["cat", "/home/alice/x.sv"]},
    "argv_option_value": {"kind": "shell", "command": ["vlog", "+incdir+/home/alice/inc"]},
    "argv_equals": {"kind": "shell", "command": ["vlog", "-f=/proj/cpu/files.f"]},
    "argv_tilde": {"kind": "shell", "command": ["cat", "~/x.sv"]},
    "argv_dotdot": {"kind": "shell", "command": ["cat", "../../etc/x"]},
    "argv_file_url": {"kind": "shell", "command": ["cat", "file:///home/alice/x.sv"]},
    "argv_fake_scheme": {"kind": "shell", "command": ["cat", "x:///home/alice"]},
    "argv_system_prefix": {"kind": "shell", "command": ["cat", "/usrx/y"]},
    "argv_install_root_sibling": {
        "kind": "shell",
        "toolchain": "questa",
        "command": [f"{TOOLS_ROOT}/questa/2025.2x/bin/v"],
    },
    "argv_dotdot_from_system": {"kind": "shell", "command": ["cat", "/usr/../home/alice/x"]},
    "argv_colon": {"kind": "shell", "command": ["x", "a:/home/alice/x"]},
    "argv_comma": {"kind": "shell", "command": ["x", "-y,/home/alice/x"]},
    "input_dotdot": {"kind": "shell", "command": ["x"], "inputs": {"a": ".."}},
    "input_tilde": {"kind": "shell", "command": ["x"], "inputs": {"a": "~/rtl/**"}},
    "output_dot": {
        "kind": "shell",
        "command": ["x"],
        "params": {"o": "."},
        "outputs": {"o": {"dir": "${params.o}"}},
    },
    "output_dotdot": {
        "kind": "shell",
        "command": ["x"],
        "params": {"o": ".."},
        "outputs": {"o": {"dir": "${params.o}"}},
    },
    "output_tilde": {
        "kind": "shell",
        "command": ["x"],
        "params": {"o": "~/x"},
        "outputs": {"o": "${params.o}"},
    },
    "argv_incdir_dotdot": {"kind": "shell", "command": ["vlog", "+incdir+../../inc"]},
    "argv_option_dotdot": {"kind": "shell", "command": ["vlog", "-I../inc"]},
    "input_absolute": {"kind": "shell", "command": ["x"], "inputs": {"a": "/proj/rtl/**"}},
    "input_escapes": {"kind": "shell", "command": ["x"], "inputs": {"a": "rtl/../../x/**"}},
    "output_absolute": {
        "kind": "shell",
        "command": ["x"],
        "params": {"out": "/nfs/proj/out.txt"},
        "outputs": {"o": "${params.out}"},
    },
    "output_escapes": {
        "kind": "shell",
        "command": ["x"],
        "params": {"out": "a/../../out.txt"},
        "outputs": {"o": "${params.out}"},
    },
    "config_file_escapes": {
        "kind": "shell",
        "command": ["x"],
        "params": {"p": "../x.ini"},
        "config_files": {"${params.p}": "x"},
    },
}


# R4
@pytest.mark.parametrize("case", sorted(BAD_PATHS))
def test_absolute_path_rejected(cas: FsCAS, tmp_path: Path, case: str) -> None:
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"relative") as exc:
        p.plan(flow({"bad": BAD_PATHS[case]}), base=tmp_path)
    assert "steps.bad" in str(exc.value)


# R4
@pytest.mark.parametrize(
    "argv",
    [
        ["/usr/bin/env", "true"],
        [f"{TOOLS_ROOT}/questa/2025.2/bin/vsim", "-c"],
        ["sh", "-c", "true", "/dev/null"],
        ["grep", "-e", "a/b", "rtl/x.sv"],
        ["fetch", "https://example.invalid/a/b", "--url=https://example.invalid/c"],
        ["vlog", "rtl/../rtl/x.sv", "a..b"],
    ],
)
def test_system_and_toolchain_paths_allowed(cas: FsCAS, tmp_path: Path, argv: list[str]) -> None:
    p, _ = planner(cas, RTL)
    plan = p.plan(
        flow({"ok": {"kind": "shell", "toolchain": "questa", "command": argv}}), base=tmp_path
    )
    assert plan.actions[0].argv == tuple(argv)


def test_imports_are_not_supported_yet(cas: FsCAS, tmp_path: Path) -> None:
    step = {"kind": "shell", "command": ["x"], "inputs": {"rtl": "${imports.rtl}/**"}}
    imports = {"rtl": {"from": "rtl-team/cpu", "channel": "stable"}}
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"imports require flow.lock support"):
        p.plan(flow({"s": step}, imports=imports), base=tmp_path)


def test_source_symlink_rejected(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL, links={"rtl/link.sv": "core.sv"})
    step = {"kind": "shell", "command": ["x"], "inputs": {"rtl": "rtl/**"}}
    with pytest.raises(PlanError, match=r"rtl/link\.sv.*symlink"):
        p.plan(flow({"s": step}), base=tmp_path)


def test_overlapping_paths_rejected(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    step = {
        "kind": "shell",
        "command": ["x"],
        "inputs": {"rtl": "rtl/**"},
        "outputs": {"o": {"dir": "rtl"}},
    }
    with pytest.raises(PlanError, match=r"input 'rtl/[a-z]+\.[a-z]+' lies inside output 'o' 'rtl'"):
        p.plan(flow({"s": step}), base=tmp_path)
    twice = {"kind": "shell", "command": ["x"], "outputs": {"a": "x.txt", "b": "./x.txt"}}
    with pytest.raises(PlanError, match=r"x\.txt"):
        p.plan(flow({"s": twice}), base=tmp_path)


def test_generated_and_declared_names_must_not_collide(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    step = {"kind": "shell", "script": "true", "config_files": {".ebs/script.sh": "x"}}
    with pytest.raises(PlanError, match=r"\.ebs/script\.sh"):
        p.plan(flow({"s": step}), base=tmp_path)


def test_declared_config_files_are_rendered(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    step = {
        "kind": "shell",
        "command": ["x"],
        "params": {"v": "7"},
        "config_files": {"cfg/${params.v}.ini": "v=${params.v}\n"},
    }
    (action,) = p.plan(flow({"s": step}), base=tmp_path).actions
    assert action.config_files == {"cfg/7.ini": "v=7\n"}


def test_make_step(cas: FsCAS, tmp_path: Path) -> None:
    plan = arch_plan(cas, tmp_path, targets=["lint_legacy"])
    (lint,) = plan.actions
    assert lint.argv == ("make", "-C", "flows/lint", "lint")
    assert lint.runtime_env == {"MAKEFLAGS": "-j$EBS_CPUS"}
    # The rule's implicit workdir input and the declared one name the same file once.
    paths = [i.logical_path for i in lint.inputs]
    assert paths == sorted(set(paths))
    assert "flows/lint/Makefile" in paths
    assert lint.toolchain is not None
    assert lint.toolchain.module == "vc_spyglass/2025.06"
    assert plan.toolchains["spyglass"] == PlanToolchain(
        "vc_spyglass/2025.06",
        lint.toolchain.id,
        {"TOOL_HOME": str(TOOLS_ROOT / "vc_spyglass/2025.06")},  # the runner sets it (P0-13)
    )


def test_toolchain_defaults_merge_with_step(cas: FsCAS, tmp_path: Path) -> None:
    toolchains = {
        "questa": {
            "module": "questa/2025.2",
            "licenses": {"msimhdlsim": 1, "qvip": 1},
            "resources": {"cpus": 4, "mem": "2G"},
        }
    }
    step = {
        "kind": "shell",
        "toolchain": "questa",
        "command": ["x"],
        "licenses": {"msimhdlsim": 2},
        "resources": {"mem": "8G"},
    }
    p, _ = planner(cas, RTL)
    (action,) = p.plan(flow({"s": step}, toolchains=toolchains), base=tmp_path).actions
    assert action.licenses == {"msimhdlsim": 2, "qvip": 1}
    assert (action.resources.cpus, action.resources.mem, action.resources.time) == (
        4,
        8 << 30,
        None,
    )


def test_unknown_kind_is_a_plan_error(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"steps\.s.*did you mean 'shell'"):
        p.plan(flow({"s": {"kind": "shel", "command": ["x"]}}), base=tmp_path)


def test_rehash_needs_a_rehashing_snapshotter(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"rehash"):
        p.plan(arch_flow(tmp_path), base=tmp_path, rehash=True)
    p, _ = planner(cas, RTL, rehash=True)
    assert p.plan(arch_flow(tmp_path), base=tmp_path, rehash=True).actions


def test_sources_are_snapshotted_once_per_pattern(cas: FsCAS, tmp_path: Path) -> None:
    p, snap = planner(cas, RTL)
    p.plan(arch_flow(tmp_path), base=tmp_path)
    assert len(snap.calls) == len(set(snap.calls))
    assert all(base == tmp_path for base, _ in snap.calls)


def _deterministic_arch(cas: FsCAS, base: Path) -> Plan:
    steps = dict(ARCH_STEPS)
    steps["compile"] = {**ARCH_STEPS["compile"], "outputs": {"worklib": {"dir": "work/${row.lib}"}}}
    return arch_plan(cas, base, steps)


# R7
def test_refine_fills_keys(cas: FsCAS, tmp_path: Path) -> None:
    plan = _deterministic_arch(cas, tmp_path)
    p, _ = planner(cas, RTL)
    (elab,) = steps_of(plan, "elab")
    assert elab.key is None
    assert all(i.id is None for i in elab.inputs)
    # elab's key is unknown, so the id of its nondeterministic output is unknown too.
    assert all(a.key is None for a in steps_of(plan, "sim"))
    assert all(a.key is not None for a in steps_of(plan, "compile"))

    produced = {(CORE, "worklib"): hash_bytes(b"core lib")}
    partial = p.refine(plan, produced)
    assert partial.action("elab").input(f"worklib/{CORE}").id == hash_bytes(b"core lib")
    assert partial.action("elab").key is None

    produced[(ALU, "worklib")] = hash_bytes(b"alu lib")
    full = p.refine(partial, produced)
    assert all(a.key is not None for a in full.actions)
    elab_key = full.action("elab").key
    assert elab_key is not None
    for sim in steps_of(full, "sim"):
        assert sim.input("model/elab").id == nondeterministic_output_id(elab_key, "model")
        assert sim.key == compute_key(sim)
    # Everything but keys and input ids is untouched.
    assert [a.action_id for a in full.actions] == [a.action_id for a in plan.actions]
    assert full.edges == plan.edges


def _all_produced() -> dict[tuple[str, str], Digest]:
    return {(CORE, "worklib"): hash_bytes(b"c"), (ALU, "worklib"): hash_bytes(b"a")}


# R7 (perf: Hypothesis' 200 ms deadline per example is a wall-clock budget for `refine`)
@pytest.mark.perf
def test_refine_idempotent(cas: FsCAS, tmp_path: Path) -> None:
    # Planning writes and snapshots sources (fsync, SQLite): ~80 ms, with spikes past 400 ms on
    # slow disks. It is done once here so the deadline times `refine` (~1 ms), not setup I/O.
    plan = _deterministic_arch(cas, tmp_path)
    p, _ = planner(cas, RTL)
    everything = _all_produced()

    @settings(max_examples=50)
    @given(
        subset=st.sets(st.sampled_from(sorted(everything))),
        more=st.sets(st.sampled_from(sorted(everything))),
    )
    def check(subset: set[tuple[str, str]], more: set[tuple[str, str]]) -> None:
        first = {k: everything[k] for k in subset}
        second = {k: everything[k] for k in subset | more}
        once = p.refine(plan, first)
        assert p.refine(once, first) == once
        assert p.refine(once, second) == p.refine(plan, second)
        assert p.refine(p.refine(plan, everything), {}) == p.refine(plan, everything)

    check()


def test_refine_rejects_unknown_producers(cas: FsCAS, tmp_path: Path) -> None:
    plan = _deterministic_arch(cas, tmp_path)
    p, _ = planner(cas, RTL)
    with pytest.raises(PlanError, match=r"nope"):
        p.refine(plan, {("nope", "worklib"): hash_bytes(b"x")})


def test_plan_metadata(cas: FsCAS, tmp_path: Path) -> None:
    plan = arch_plan(cas, tmp_path)
    assert (plan.domain, plan.project, plan.lock) == ("cpu-nda", "rv32x-cpu", None)
    assert set(plan.toolchains) == {"questa", "spyglass"}
    assert list(plan.edges) == sorted(plan.edges)
    assert plan.flow.path == "flow.yaml"


# R11
@pytest.mark.slow
@pytest.mark.perf
def test_large_plan_perf(tmp_path: Path) -> None:
    cas = FsCAS(tmp_path / "cas", "test")
    libs = "lib,filelist\n" + "".join(f"l{i},l{i}.f\n" for i in range(50))
    tests = "test,seed,plusargs,timeout\n" + "".join(
        f"t{i % 40},{i},+x={i},10m\n" for i in range(2000)
    )
    files = {f"rtl/l{i}.f": f"l{i}.sv\n" for i in range(50)} | {
        f"rtl/l{i}.sv": "m\n" for i in range(50)
    }
    base = tmp_path / "flow"
    write_tables(base, {"libs.csv": libs, "tests/regression.csv": tests})
    f = flow(
        {k: ARCH_STEPS[k] for k in ("compile", "elab", "sim")},
        toolchains={"questa": {"module": "questa/2025.2"}},
    )
    p = Planner(cas, FakeSnapshotter(cas, files), FakeToolchains(), registry())
    p.plan(f, base=base)  # warm-up: snapshots land in the CAS, imports are loaded

    p = Planner(cas, FakeSnapshotter(cas, files), FakeToolchains(), registry())
    start = time.perf_counter()
    plan = p.plan(f, base=base)
    elapsed = time.perf_counter() - start
    assert len(plan.actions) == 50 + 1 + 2000
    assert all(a.key is not None for a in plan.actions)
    assert elapsed < 2.0, f"planning took {elapsed:.2f} s"

    tracemalloc.start()
    try:
        Planner(cas, FakeSnapshotter(cas, files), FakeToolchains(), registry()).plan(f, base=base)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 300 * 2**20, f"planning peaked at {peak / 2**20:.0f} MiB"


def test_real_snapshotter_plugs_in(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("module a; endmodule\n")
    with StatCache(tmp_path / "stat.sqlite") as cache:
        sources: Snapshotter = SourceSnapshotter(cas, cache, clock=fake_clock, git=None)  # mypy
        p = Planner(cas, sources, FakeToolchains(), registry())
        step = {"kind": "shell", "command": ["cat", "rtl/a.sv"], "inputs": {"rtl": "rtl/*.sv"}}
        (action,) = p.plan(flow({"s": step}), base=tmp_path).actions
    assert action.inputs == (
        InputRef(
            "rtl/a.sv",
            "file",
            SourceInput("rtl", "rtl/*.sv"),
            hash_bytes(b"module a; endmodule\n"),
        ),
    )


def test_source_and_output_at_the_same_path_rejected(cas: FsCAS, tmp_path: Path) -> None:
    files = {**RTL, "model/elab": "not a model\n"}
    inputs = {"model": "${steps.elab.outputs.model}", "x": "model/elab"}
    steps = {**ARCH_STEPS, "sim": {**ARCH_STEPS["sim"], "inputs": inputs}}
    p, _ = planner(cas, files)
    with pytest.raises(PlanError, match=r"two different inputs are staged at 'model/elab'"):
        p.plan(arch_flow(tmp_path, steps), base=tmp_path)


# R7: a deterministic producer that reran (new bytes) replaces the id it gave before.
def test_refine_replaces_stale_content_id(cas: FsCAS, tmp_path: Path) -> None:
    plan = _deterministic_arch(cas, tmp_path)
    p, _ = planner(cas, RTL)
    d1, d2, x = hash_bytes(b"1"), hash_bytes(b"2"), hash_bytes(b"x")
    first = p.refine(plan, {(CORE, "worklib"): d1, (ALU, "worklib"): x})
    second = p.refine(first, {(CORE, "worklib"): d2, (ALU, "worklib"): x})
    assert second.action("elab").input(f"worklib/{CORE}").id == d2
    assert second == p.refine(plan, {(CORE, "worklib"): d2, (ALU, "worklib"): x})
    assert second.action("elab").key != first.action("elab").key


class _RacySnapshotter(FakeSnapshotter):
    """Returns different bytes for one file depending on the pattern (edited between calls)."""

    def snapshot(self, base: Path, pattern: str, *, optional: bool = False) -> SnapshotResult:
        self.files["rtl/a.sv"] = f"version for {pattern}\n"
        return super().snapshot(base, pattern, optional=optional)


def test_same_path_different_digest_rejected(cas: FsCAS, tmp_path: Path) -> None:
    snap = _RacySnapshotter(cas, {"rtl/a.sv": ""})
    p = Planner(cas, snap, FakeToolchains(), registry())
    step = {"kind": "shell", "command": ["x"], "inputs": {"a": "rtl/**", "b": "rtl/*.sv"}}
    with pytest.raises(PlanError, match=r"two different inputs are staged at 'rtl/a\.sv'"):
        p.plan(flow({"s": step}), base=tmp_path)


# R1
def test_multi_key_selector(cas: FsCAS, tmp_path: Path) -> None:
    pick = {"kind": "shell", "command": ["x", "${steps.sim.outputs.result[test=smoke,seed=2]}"]}
    plan = arch_plan(cas, tmp_path, with_step("pick", pick))
    path = "result/sim[plusargs=%2Bverbose,seed=2,test=smoke,timeout=10m]/result.json"
    action = plan.action("pick")
    assert action.argv == ("x", path)
    assert [i.logical_path for i in action.inputs] == [path]


# R1
def test_selector_ambiguous_rejected(cas: FsCAS, tmp_path: Path) -> None:
    pick = {"kind": "shell", "command": ["x", "${steps.sim.outputs.result[test=smoke]}"]}
    with pytest.raises(PlanError, match=r"selector matches 2 instances"):
        arch_plan(cas, tmp_path, with_step("pick", pick))
