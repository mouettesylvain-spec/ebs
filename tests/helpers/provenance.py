"""A provenance scenario shared by the contract and CLI tests of P2-04.

An RTL build (gen -> compile) released as `rtl/cpu 1.0`; a verification build that consumes it
(elab, two sim seeds, lint) and releases `verif/model 1.0`; an SoC build that consumes that
release and a later rerun of one seed; `rtl/cpu 1.0` exported to domain `other`, where a build
simulates on it; and a build in domain `third` holding the same id without any export.
"""

from __future__ import annotations

from dataclasses import dataclass

from ebs.core.clock import FakeClock
from ebs.core.digest import Digest, hash_bytes
from ebs.meta.api import BuildId, ReleaseCreate, ReleaseOutput, ReleaseView, release_output_name
from ebs.release.releases import ReleaseBackend
from tests.helpers.release import Action, finish, out

D, OTHER, THIRD = "test", "other", "third"
SRC_A = hash_bytes(b"module a; endmodule")
SRC_B = hash_bytes(b"module b; endmodule")
TC = hash_bytes(b"toolchain questa")
FL = out(b"filelist")
LIB = out(b"worklib", deterministic=False)
MODEL = out(b"model", deterministic=False)
REPORT = out(b"lint report")
RES = {name: out(name.encode()) for name in ("t1-1", "t1-2", "soc-t1-1", "soc-t1-1-rerun", "o")}


@dataclass
class S:
    store: ReleaseBackend
    clock: FakeClock


def sim(test: str, seed: int, model: Digest, result: str, *, passed: bool = True) -> Action:
    return Action(
        {"result": RES[result]}, passed=passed, inputs={"model": model}, rule="questa.sim",
        params={"test": test, "seed": seed}, toolchain=TC,
    )  # fmt: skip


def release(s: S, build: BuildId, name: str, action_id: str, output: str) -> ReleaseView:
    result = s.store.get_result(build, action_id)
    assert result is not None
    o = result.outputs[output]
    return s.store.create_release(
        ReleaseCreate(
            domain=s.store.get_build(build).domain, name=name, version="1.0", build=build,
            outputs={release_output_name(action_id, output): ReleaseOutput(
                action_id=action_id, output=output, **o.model_dump())},
        ),
        by="alice",
    )  # fmt: skip


@dataclass
class World:
    rtl: BuildId
    verif: BuildId
    soc: BuildId
    rerun: BuildId
    other: BuildId
    third: BuildId
    rtl_release: ReleaseView
    verif_release: ReleaseView
    exported: ReleaseView


def world(s: S) -> World:
    store = s.store
    rtl = finish(store, {
        "gen": Action({"filelist": FL}, inputs={"src/a.sv": SRC_A}, rule="shell"),
        "compile[lib=core]": Action(
            {"worklib": LIB}, inputs={"srcs/a.sv": SRC_A, "srcs/b.sv": SRC_B, "filelist": FL.id},
            rule="questa.compile", params={"lib": "core", "vlog_opts": "-sv"}, toolchain=TC,
        ),
    }, project="rtl", flow_repo="git@example.invalid:rtl.git", flow_commit="abc123")  # fmt: skip
    rtl_release = release(s, rtl, "rtl/cpu", "compile[lib=core]", "worklib")
    s.clock.advance(60)
    verif = finish(store, {
        "elab": Action({"model": MODEL}, inputs={"libs/core": LIB.id}, rule="questa.opt",
                       params={"top": "tb_top"}, toolchain=TC),
        "sim[seed=1,test=t1]": sim("t1", 1, MODEL.id, "t1-1"),
        "sim[seed=2,test=t1]": sim("t1", 2, MODEL.id, "t1-2", passed=False),
        "lint": Action({"report": REPORT}, inputs={"libs/core": LIB.id}, rule="shell"),
    }, project="verif", flow_commit="def456", status="failed")  # fmt: skip
    verif_release = release(s, verif, "verif/model", "elab", "model")
    s.clock.advance(60)
    soc = finish(store, {"sim[seed=1,test=t1]": sim("t1", 1, MODEL.id, "soc-t1-1", passed=False)},
                 project="soc")  # fmt: skip
    s.clock.advance(60)
    rerun = finish(store, {"sim[seed=1,test=t1]": sim("t1", 1, MODEL.id, "soc-t1-1-rerun")},
                   project="soc")  # fmt: skip
    exported = store.record_export(rtl_release.id, OTHER, by="carol")
    s.clock.advance(60)
    other = finish(store, {"sim[seed=7,test=t9]": Action(
        {"result": RES["o"]}, inputs={"libs/core": LIB.id}, rule="questa.sim",
        params={"test": "t9", "seed": 7}, toolchain=TC)}, domain=OTHER, project="o")  # fmt: skip
    # Domain `third` holds the same id but never received an export: no query may link it.
    third = finish(store, {"sim[seed=1,test=x]": Action(
        {"result": out(b"x")}, inputs={"libs/core": LIB.id}, rule="questa.sim",
        params={"test": "x", "seed": 1})}, domain=THIRD, project="x")  # fmt: skip
    return World(rtl, verif, soc, rerun, other, third, rtl_release, verif_release, exported)
