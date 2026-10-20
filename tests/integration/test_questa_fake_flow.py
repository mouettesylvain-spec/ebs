"""The Questa rule pack end to end with the fake tools (task P1-08 R1-R5, R7): compile -> opt ->
sim through the driver, the local executor and real `ebs-runner` processes, on PostgreSQL.

The flow compiles a package library, then two libraries that import it (fan-in through
modelsim.ini and `-L`; the fake compilers fail on an unmapped or unsearched library), optimizes
them into a model and simulates a regression whose rows pick the fake simulator's behaviour
with a `+FAKE_QUESTA_MODE=<mode>` plusarg.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import URL

from ebs.cas.fs import FsCAS
from ebs.cli._context import SiteServices
from ebs.cli._output import Output
from ebs.cli.plan import make_plan
from ebs.core.digest import Digest
from ebs.driver.build import run_build
from ebs.driver.retry import BackoffRetry
from ebs.driver.scheduler import DriverConfig
from ebs.meta.api import BuildId, MetadataStore, ResultManifest
from ebs.meta.models import events
from tests.helpers.e2e import BuildRun, Site, parse_build
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url
from tests.helpers.questa import FAKES_BIN

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

DOMAIN = "test"
TOOL_PATH = f"{FAKES_BIN}:/usr/bin:/bin"
LIBS_CSV = "lib,dir,filelist\ncore,rtl/core,rtl/core/core.f\ntb,tb,tb/tb.f\n"
REGRESSION_CSV = (
    "test,seed,plusargs,timeout\n"
    "smoke,1,,1m\n"
    "rand,2,+FAKE_QUESTA_MODE=uvm_error,1m\n"
    "rand,3,+FAKE_QUESTA_MODE=uvm_error,1m\n"
    "assert,4,+FAKE_QUESTA_MODE=fatal,1m\n"
    "stuck,5,+FAKE_QUESTA_MODE=no_finish,1m\n"
)
INFRA_CSV = (
    "test,seed,plusargs,timeout\n"
    "smoke,1,,1m\n"
    "lic,2,+FAKE_QUESTA_MODE=license_fail,1m\n"
    "crash,3,+FAKE_QUESTA_MODE=segv,1m\n"
    "hang,4,+FAKE_QUESTA_MODE=hang,2s\n"
)
SOURCES = {
    "rtl/pkg/types_pkg.sv": "package types_pkg;\n  typedef logic [7:0] byte_t;\nendpackage\n",
    "rtl/pkg/pkg.f": "rtl/pkg/types_pkg.sv\n",
    "rtl/core/alu.sv": (
        "import types_pkg::*;\n// adds two bytes\nmodule alu(input byte_t a, b, output byte_t y);\n"
        "  assign y = a + b;\nendmodule\n"
    ),
    "rtl/core/core.f": "rtl/core/alu.sv\n",
    "tb/tb_top.sv": "import types_pkg::*;\nmodule tb_top;\n  alu dut();\nendmodule\n",
    "tb/tb.f": "tb/tb_top.sv\n",
}
COMPILES = {"pkg", "compile[lib=core]", "compile[lib=tb]"}
SCB_SIGNATURE = (
    "UVM_ERROR <path>(<n>) @ <n>: uvm_test_top.env.sb [SCB] data mismatch at <hex>: "
    "expected <hex> got <hex>"
)


def flow_yaml(regression: str = "tests.csv") -> str:
    env = {"PATH": TOOL_PATH}
    return f"""\
version: 1
project: fake-cpu
domain: {DOMAIN}
steps:
  pkg:
    kind: questa.compile
    inputs: {{ srcs: "rtl/pkg/*.sv", filelist: rtl/pkg/pkg.f }}
    params: {{ lib: pkg, vlog_opts: "-sv" }}
    env: {json.dumps(env)}
  compile:
    kind: questa.compile
    matrix: {{ table: libs.csv, id: [lib] }}
    inputs:
      srcs: "${{row.dir}}/*.sv"
      filelist: "${{row.filelist}}"
      deps: "${{steps.pkg.outputs.worklib}}"
    params: {{ lib: "${{row.lib}}", vlog_opts: "-sv -timescale 1ns/1ps" }}
    env: {json.dumps(env)}
  elab:
    kind: questa.opt
    inputs:
      pkg: "${{steps.pkg.outputs.worklib}}"
      libs: "${{steps.compile.outputs.worklib[*]}}"
    params: {{ top: tb_top, vopt_opts: "+acc=npr" }}
    outputs: {{ model: {{ dir: opt, deterministic: false }} }}
    env: {json.dumps(env)}
  sim:
    kind: questa.sim
    matrix: {{ table: {regression}, id: [test, seed] }}
    inputs:
      model: "${{steps.elab.outputs.model}}"
      pkg: "${{steps.pkg.outputs.worklib}}"
      libs: "${{steps.compile.outputs.worklib[*]}}"
    params: {{ test: "${{row.test}}", seed: "${{row.seed}}", plusargs: "${{row.plusargs}}" }}
    outputs: {{ result: result.json, cov: {{ file: cov.ucdb, optional: true }} }}
    resources: {{ time: "${{row.timeout}}" }}
    env: {json.dumps({**env, "FAKE_QUESTA_MODE": "pass"})}
    debug: {{ collect: [transcript] }}
"""


def _project(root: Path, regression: str = REGRESSION_CSV) -> Path:
    root.mkdir(parents=True)
    files = {
        **SOURCES,
        "libs.csv": LIBS_CSV,
        "tests.csv": regression,
        "flow.yaml": flow_yaml(),
    }
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def _sim_ids(regression: str) -> set[str]:
    rows = [line.split(",") for line in regression.splitlines()[1:]]
    return {f"sim[seed={r[1]},test={r[0]}]" for r in rows}


@pytest.fixture
def site(tmp_path: Path, pg_url: URL) -> Site:
    # A short kill grace keeps the `hang` row (2 s time limit) quick. Questa steps default to
    # `sandbox: auto` (P3-01): where bwrap works the fake tools must be visible inside it.
    return Site(
        tmp_path / "site",
        pg_url,
        extra_config=(
            "[runner]\nkill_grace_s = 1\n"
            f"[sandbox]\nro_binds = [{json.dumps(str(FAKES_BIN.parent))}]\n"
        ),
    )


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    return _project(tmp_path / "proj")


def _build(site: Site, proj: Path, *args: str) -> BuildRun:
    proc = site.ebs(proj, "build", "--json", "-k", *args)
    assert proc.stdout.strip(), proc.stderr
    return parse_build(proc)


def _store(site: Site) -> MetadataStore:
    services = SiteServices(environ=site.env(), cwd=site.root, home=site.root / "home")
    store = services.store(required=True)
    assert store is not None
    return store


def _result(store: MetadataStore, run: BuildRun, action_id: str) -> ResultManifest:
    result = store.get_result(BuildId(run.result["build"]), action_id)
    assert result is not None, action_id
    return result


def _output_bytes(site: Site, result: ResultManifest, name: str) -> bytes:
    cas = FsCAS(site.root / "cas", DOMAIN)
    with cas.open(result.outputs[name].digest) as f:
        return f.read()


def _result_json(site: Site, result: ResultManifest) -> dict[str, Any]:
    doc = json.loads(_output_bytes(site, result, "result"))
    assert isinstance(doc, dict)
    return doc


# R1, R2, R3, R4, R7
def test_regression_results_and_caching(site: Site, proj: Path) -> None:
    first = _build(site, proj, "--cache", "write")
    sims = _sim_ids(REGRESSION_CSV)
    assert first.submitted == COMPILES | {"elab"} | sims, first.proc.stderr
    assert first.result["counts"] == {"done": 5, "failed": 4}, first.proc.stderr

    store = _store(site)
    smoke = _result(store, first, "sim[seed=1,test=smoke]")
    assert smoke.status == "passed"
    doc = _result_json(site, smoke)
    assert doc["status"] == "passed"
    assert (doc["test"], doc["seed"], doc["first_error"]) == ("smoke", "1", None)
    assert smoke.summary["status"] == "passed"
    assert b"test=smoke seed=1" in _output_bytes(site, smoke, "cov")  # -coverage + save

    # R4: both seeds of a UVM failure cluster under one signature; the simulator exited 0
    for seed in (2, 3):
        failed = _result(store, first, f"sim[seed={seed},test=rand]")
        assert (failed.status, failed.exit_code) == ("failed", 0)
        assert failed.summary["signature"] == SCB_SIGNATURE
        assert _result_json(site, failed)["uvm_errors"] == 1
    assertion = _result(store, first, "sim[seed=4,test=assert]")
    assert _result_json(site, assertion)["fatals"] == 1
    stuck = _result(store, first, "sim[seed=5,test=stuck]")
    assert (stuck.status, _result_json(site, stuck)["finished"]) == ("failed", False)

    # Test failures are results: the second run is all cache hits, nothing is submitted.
    second = _build(site, proj, "--cache", "write")
    assert second.submitted == set()
    assert second.cached == COMPILES | {"elab"} | sims


# R1, R7
def test_rtl_change_recompiles_only_its_library(site: Site, proj: Path) -> None:
    first = _build(site, proj, "--cache", "write")
    store = _store(site)

    # R7: libraries embed timestamps, so a forced rerun gives different bytes; downstream
    # actions key on the producer's action key, so their inputs (and keys) do not move.
    rerun = _build(site, proj, "--no-cache")
    assert rerun.submitted == first.submitted
    pkg_before = _result(store, first, "pkg").outputs["worklib"]
    pkg_after = _result(store, rerun, "pkg").outputs["worklib"]
    assert pkg_before.digest != pkg_after.digest
    assert pkg_before.id == pkg_after.id
    elab = [_result(store, run, "elab") for run in (first, rerun)]
    assert elab[0].action_key == elab[1].action_key
    assert elab[0].inputs == elab[1].inputs
    # result.json holds no timestamps: an identical outcome is byte-identical (early cutoff)
    smoke = [_result(store, run, "sim[seed=1,test=smoke]") for run in (first, rerun)]
    assert smoke[0].outputs["result"].digest == smoke[1].outputs["result"].digest

    tb = proj / "tb/tb_top.sv"
    tb.write_text(tb.read_text().replace("module tb_top;", "module tb_top; // edited"))
    edited = _build(site, proj, "--cache", "write")
    assert edited.cached == {"pkg", "compile[lib=core]"}
    assert edited.submitted == {"compile[lib=tb]", "elab"} | _sim_ids(REGRESSION_CSV)


# R1: a compile whose upstream library is not mapped fails as a test failure would (cached)
def test_unmapped_library_fails_compile(site: Site, proj: Path) -> None:
    flow = proj / "flow.yaml"
    flow.write_text(flow_yaml().replace('      deps: "${steps.pkg.outputs.worklib}"\n', ""))
    run = _build(site, proj, "--cache", "write")
    assert run.result["status"] == "failed"
    store = _store(site)
    for lib in ("core", "tb"):
        result = _result(store, run, f"compile[lib={lib}]")
        assert result.status == "failed"
        assert result.log is not None
        with FsCAS(site.root / "cas", DOMAIN).open(result.log) as f:
            assert b"Package 'types_pkg' is not in the work library" in f.read()


def _infra_events(pg: URL, build: BuildId) -> dict[str, list[tuple[str, str | None]]]:
    """infra_failed events (from the runner and the driver): action -> [(reason, detail)]."""
    engine = sa.create_engine(pg, poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sa.select(events.c.action_id, events.c.data)
                .where(events.c.build_id == int(build), events.c.type == "infra_failed")
                .order_by(events.c.id)
            ).all()
    finally:
        engine.dispose()
    found: dict[str, list[tuple[str, str | None]]] = {}
    for action_id, data in rows:
        found.setdefault(action_id, []).append((data["reason"], data.get("detail")))
    return found


# R5, R7: license failures, crashes and hangs are infrastructure failures: retried, never cached
def test_infra_failures_are_retried_never_cached(site: Site, tmp_path: Path, pg_url: URL) -> None:
    proj = _project(tmp_path / "infra", INFRA_CSV)
    services = SiteServices(environ=site.env(), cwd=proj, home=site.root / "home")
    out = Output(json_mode=True, tty=False, no_color=True)
    planned = make_plan(services, out, proj / "flow.yaml")
    store = services.store(required=True)
    assert store is not None
    with services.open_executor("local", domain=DOMAIN, log_dir=proj / ".ebs" / "logs") as ex:
        outcome = run_build(
            planned.plan,
            refiner=planned.planner,
            cas=planned.cas,
            store=store,
            executor=ex,
            workdir=proj,
            user="alice",
            config=DriverConfig(
                cache_mode="write", keep_going=True, poll_min_s=0.05, poll_max_s=0.2
            ),
            retry=BackoffRetry(max_retries=1, base_s=0),
        )
    infra = {"sim[seed=2,test=lic]", "sim[seed=3,test=crash]", "sim[seed=4,test=hang]"}
    assert {a for a, s in outcome.states.items() if s == "infra_failed"} == infra
    assert outcome.states["sim[seed=1,test=smoke]"] == "done"
    for action_id in infra:
        assert store.get_result(outcome.build, action_id) is None  # no result, no cache entry
        key = outcome.plan.action(action_id).key
        assert isinstance(key, Digest)
        assert store.cache_get(DOMAIN, key) is None

    # Each attempt (the first and the one retry) failed for the rule's reason; the runner's
    # event carries the rule's classification as its detail.
    found = _infra_events(pg_url, outcome.build)
    lic = found["sim[seed=2,test=lic]"]
    assert {reason for reason, _ in lic} == {"license"}
    assert [d for _, d in lic].count("license") == 2
    crash = found["sim[seed=3,test=crash]"]
    assert {reason for reason, _ in crash} == {"other"}
    assert [d for _, d in crash].count("tool_crash") == 2
    hang = found["sim[seed=4,test=hang]"]
    assert {reason for reason, _ in hang} == {"timeout"}
    assert len(hang) >= 2


# R7: the fake tools are plain executables any shell can run (no ebs import)
def test_fake_tools_run_standalone(tmp_path: Path) -> None:
    env = {"PATH": TOOL_PATH}
    script = "vlib work/x && printf 'module m; endmodule\\n' > m.sv && vlog -work work/x m.sv"
    proc = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "work/x/m/_primary.dat").is_file()
    before = (tmp_path / "work/x/_info").read_bytes()
    subprocess.run(shlex.split("vlib work/x"), cwd=tmp_path, env=env, check=True)
    assert (tmp_path / "work/x/_info").read_bytes() != before  # timestamps: bytes differ
    assert os.access(FAKES_BIN / "vsim", os.X_OK)
