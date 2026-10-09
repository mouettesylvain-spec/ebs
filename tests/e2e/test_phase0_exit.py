"""Phase 0 exit criterion (P0-17): a lint Makefile wrapped as-is gets a 100 % cache hit on the
second run, plus early cutoff, minimal reruns and plan-time source snapshots, on `examples/`."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from tests.helpers.e2e import BIN_DIR, Site, copy_example, parse_build
from tests.helpers.pg import _pg_truncate_engine, pg_migrated, pg_server, pg_url

__all__ = ["_pg_truncate_engine", "pg_migrated", "pg_server", "pg_url"]

LINT_ACTIONS = {"lint[block=alu]", "lint[block=counter]", "summary"}
ALU = Path("rtl/alu/alu.sv")
COMMENT = "// Combinational ALU: add, subtract, and, or."
UNUSED_DECL = "  logic unused_dbg;\n"


@pytest.fixture
def site(tmp_path: Path, pg_url: URL) -> Site:
    return Site(tmp_path / "site", pg_url)


@pytest.fixture
def lint_make(tmp_path: Path) -> Path:
    return copy_example("lint-make", tmp_path)


def _add_unused_signal(proj: Path) -> None:
    alu = proj / ALU
    text = alu.read_text()
    marker = "  always_comb begin\n"
    assert marker in text
    alu.write_text(text.replace(marker, UNUSED_DECL + marker, 1))


def _logs(site: Site, proj: Path, action: str) -> str:
    proc = site.ebs(proj, "logs", action)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


# R1
def test_second_run_fully_cached(site: Site, lint_make: Path) -> None:
    first = site.build(lint_make)
    assert first.submitted == LINT_ACTIONS
    assert first.result["counts"] == {"done": 3}

    second = site.build(lint_make)
    assert second.submitted == set()  # nothing reaches the executor
    assert second.cached == LINT_ACTIONS
    assert second.result["counts"] == {"cached": 3}
    assert "0 violation(s)" in _logs(site, lint_make, "summary")


# R2
def test_early_cutoff_comment_change(site: Site, lint_make: Path) -> None:
    site.build(lint_make)
    alu = lint_make / ALU
    text = alu.read_text()
    assert COMMENT in text
    alu.write_text(text.replace(COMMENT, "// Combinational ALU (add/sub/and/or), no state."))

    rerun = site.build(lint_make)
    # the edited block's lint reruns; its normalized report is byte-identical, so the
    # downstream summary keeps its key and hits the cache (early cutoff)
    assert rerun.submitted == {"lint[block=alu]"}
    assert rerun.cached == {"lint[block=counter]", "summary"}


# R3
def test_minimal_rerun_set(site: Site, lint_make: Path) -> None:
    site.build(lint_make)
    _add_unused_signal(lint_make)

    rerun = site.build(lint_make)
    assert rerun.submitted == {"lint[block=alu]", "summary"}
    assert rerun.cached == {"lint[block=counter]"}
    summary = _logs(site, lint_make, "summary")
    assert "rtl/alu/alu.sv:10: UNUSED: signal 'unused_dbg' is never used" in summary
    assert "1 violation(s)" in summary


# R3 R6: the second example; a table edit reruns only its row and the aggregate
def test_verilator_sim_seed_change_reruns_one_test(site: Site, tmp_path: Path) -> None:
    proj = copy_example("verilator-sim", tmp_path)
    first = site.build(proj)
    sims = {"sim[seed=1,test=smoke]", "sim[seed=7,test=random]", "sim[seed=42,test=random]"}
    assert first.submitted == {"compile", "report", *sims}
    assert site.build(proj).submitted == set()

    table = proj / "tests.csv"
    rows = table.read_text()
    assert ",7\n" in rows
    table.write_text(rows.replace(",7\n", ",8\n"))

    rerun = site.build(proj)
    assert rerun.submitted == {"sim[seed=8,test=random]", "report"}
    assert rerun.cached == {"compile", "sim[seed=1,test=smoke]", "sim[seed=42,test=random]"}
    assert "3 passed, 0 failed" in _logs(site, proj, "report")


_GATE = """\
#!/bin/bash
# Test wrapper (P0-17 R4): blocks until the test has edited the sources, then lints.
: > {started}
for _ in $(seq 1200); do [ -e {gate} ] && break; sleep 0.05; done
[ -e {gate} ] || {{ echo "gate never opened" >&2; exit 3; }}
"""


def _wait_for(path: Path, proc: subprocess.Popen[str], timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert proc.poll() is None, "build ended before the tool started"
        assert time.monotonic() < deadline, f"{path} never appeared"
        time.sleep(0.05)


# R4
def test_edit_during_build_isolated(site: Site, lint_make: Path, tmp_path: Path) -> None:
    started, gate = tmp_path / "started", tmp_path / "gate"
    tools = lint_make / "tools"
    linter = (tools / "fake-lint").read_text().removeprefix("#!/bin/bash\n")
    gated = _GATE.format(started=shlex.quote(str(started)), gate=shlex.quote(str(gate)))
    (tools / "fake-lint").write_text(gated + linter)
    os.chmod(tools / "fake-lint", 0o755)

    proc = subprocess.Popen(
        [str(BIN_DIR / "ebs"), "build", "--cache", "write", "--json"], cwd=lint_make,
        env=site.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        _wait_for(started, proc)  # the plan (and its source snapshot) is done; a lint runs
        _add_unused_signal(lint_make)
        # `summary` stages only after both lints end, so after this edit whatever the order
        with (tools / "summarize").open("a") as f:
            f.write("echo EDITED\n")
        gate.touch()
        stdout, stderr = proc.communicate(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    during = parse_build(subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr))
    assert proc.returncode == 0, stdout + stderr
    assert during.submitted == LINT_ACTIONS
    # the build linted the bytes snapshotted at plan time, not the live edit
    summary = _logs(site, lint_make, "summary")
    assert "0 violation(s)" in summary
    assert "EDITED" not in summary

    after = site.build(lint_make)
    assert after.submitted == {"lint[block=alu]", "summary"}
    summary = _logs(site, lint_make, "summary")
    assert "1 violation(s)" in summary
    assert "EDITED" in summary


# R6: nightly with real Verilator (`EBS_E2E_VERILATOR=1`, see .github/workflows/nightly.yml)
@pytest.mark.parametrize("example", ["lint-make", "verilator-sim"])
def test_examples_with_real_verilator(site: Site, tmp_path: Path, example: str) -> None:
    if os.environ.get("EBS_E2E_VERILATOR") != "1":
        pytest.skip("real Verilator runs nightly: set EBS_E2E_VERILATOR=1 to run it here")
    verilator = shutil.which("verilator")
    if verilator is None:
        pytest.skip("missing dependency: verilator on PATH (EBS_E2E_VERILATOR=1 asks for it)")
    proj = copy_example(example, tmp_path)
    write_verilator_toolchain(proj, Path(verilator))

    first = site.ebs(proj, "build", "--cache", "write", "--json", "-f", "flow.verilator.yaml")
    assert first.returncode == 0, first.stdout + first.stderr
    second = site.build(proj, "-f", "flow.verilator.yaml")
    assert second.submitted == set()
    assert second.cached == parse_build(first).submitted
    # the tool really ran: a finding only Verilator's normalized output can contain
    if example == "lint-make":
        _add_unused_signal(proj)
        site.build(proj, "-f", "flow.verilator.yaml")
        assert "rtl/alu/alu.sv:10: UNUSEDSIGNAL: " in _logs(site, proj, "summary")
    else:
        assert "3 passed, 0 failed" in _logs(site, proj, "report")


def write_verilator_toolchain(proj: Path, verilator: Path) -> None:
    """`.ebs/toolchains.yaml` for the `verilator/system` module (docs/user/quickstart.md)."""
    env = {"PATH": "/usr/bin:/bin"}
    version = subprocess.run(
        [str(verilator), "--version"], capture_output=True, text=True, check=True, env=env
    ).stdout.split()[1]
    root = subprocess.run(
        [str(verilator), "--getenv", "VERILATOR_ROOT"], capture_output=True, text=True,
        check=True, env=env,
    ).stdout.strip()  # fmt: skip
    assert re.fullmatch(r"[0-9.]+", version), version
    (proj / ".ebs").mkdir(exist_ok=True)
    (proj / ".ebs" / "toolchains.yaml").write_text(
        "version: 1\ntoolchains:\n  verilator/system:\n"
        f'    version: "{version}"\n'
        f'    install_roots: ["{root}"]\n'
        f'    env: {{ PATH: "{verilator.parent}:/usr/bin:/bin", VERILATOR_ROOT: "{root}" }}\n'
    )
