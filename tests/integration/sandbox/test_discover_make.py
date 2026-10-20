"""`ebs discover` on a real make step under real strace (task P3-02 R1, R2, R4).

Hosts without strace skip these tests; the nightly job sets `EBS_REQUIRE_STRACE=1`, which turns
the skip into a failure there.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from ebs.core.errors import ExitCode
from tests.helpers.cli import Site, TestServices, write_site

FLOW = """\
version: 1
project: demo
domain: test
steps:
  lint:
    kind: make
    workdir: flows/lint
    target: lint
    inputs: { rtl: "rtl/**" }
    outputs: { report: flows/lint/report.txt }
"""


@pytest.fixture(autouse=True)
def strace_available() -> None:
    if shutil.which("strace") is None:
        if os.environ.get("EBS_REQUIRE_STRACE") == "1":
            pytest.fail("EBS_REQUIRE_STRACE=1 but strace is not on PATH")
        pytest.skip("strace is not on PATH (set EBS_REQUIRE_STRACE=1 to fail)")


def _site(tmp_path: Path, recipe: str) -> Site:
    write_site(tmp_path, flow=FLOW)
    proj = tmp_path / "proj"
    (proj / "rtl" / "core").mkdir(parents=True)
    (proj / "rtl" / "core" / "alu.sv").write_text("module alu; endmodule\n")
    (proj / "rtl" / "core" / "regs.sv").write_text("module regs; endmodule\n")
    (proj / "rtl" / "unused.sv").write_text("module unused; endmodule\n")
    (proj / "flows" / "lint").mkdir(parents=True)
    (proj / "flows" / "lint" / "Makefile").write_text(f"lint:\n\t{recipe} > report.txt\n")
    services = TestServices(tmp_path)
    services.environ["PATH"] = os.environ["PATH"]  # where strace is
    return Site(tmp_path, services)


def _discover(site: Site, *args: str, code: ExitCode) -> dict[str, Any]:
    result = site.invoke(["discover", "lint", "--json", *args])
    assert result.exit_code == code, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


# R4: only declared reads -> the audit passes; an undeclared read -> it fails, naming the file.
def test_audit_exit_code(tmp_path: Path) -> None:
    clean = _site(tmp_path / "clean", "cat ../../rtl/core/alu.sv ../../rtl/core/regs.sv")
    doc = _discover(clean, "--audit", code=ExitCode.OK)
    assert doc["exit_code"] == 0
    assert doc["undeclared"] == []
    assert doc["counts"]["input"] >= 3  # Makefile, alu.sv, regs.sv

    secret = tmp_path / "outside" / "secret.vh"
    secret.parent.mkdir()
    secret.write_text("`define SECRET 1\n")
    leaky = _site(tmp_path / "leaky", f"cat ../../rtl/core/alu.sv {secret}")
    doc = _discover(leaky, "--audit", code=ExitCode.ACTIONS_FAILED)
    assert [u["path"] for u in doc["undeclared"]] == [os.path.realpath(secret)]
    assert doc["undeclared"][0]["process"].endswith("/cat")
    # Without --audit the same trace is only a report.
    _discover(leaky, code=ExitCode.OK)


# R1, R2: a file of the flow directory that was not staged is undeclared; the patch tightens the
# glob to the files read, adds the missing one, and the flow changes only with --write.
def test_patch_and_write(tmp_path: Path) -> None:
    site = _site(tmp_path, "cat ../../rtl/core/alu.sv ../../rtl/core/regs.sv ../../common/defs.mk")
    (site.proj / "common").mkdir()
    (site.proj / "common" / "defs.mk").write_text("X = 1\n")
    flow = site.proj / "flow.yaml"
    before = flow.read_text()
    doc = _discover(site, code=ExitCode.OK)
    assert doc["exit_code"] != 0  # cat failed on the unstaged file
    assert [(u["path"], u["missing"]) for u in doc["undeclared"]] == [
        (os.path.realpath(site.proj / "common" / "defs.mk"), True)
    ]
    # cat failed, so the trace is partial: declared inputs are kept as written.
    assert doc["proposal"]["inputs"] == {"rtl": "rtl/**", "discovered": "common/defs.mk"}
    assert flow.read_text() == before  # never edits without --write
    assert not list((tmp_path / "scratch").glob("repro-*/*"))  # the sandbox was removed

    _discover(site, "--write", code=ExitCode.OK)
    after = _discover(site, "--audit", code=ExitCode.OK)  # the patched flow declares everything
    assert after["exit_code"] == 0
    assert after["undeclared"] == []
    # A complete trace tightens the glob to the files read.
    _discover(site, "--write", code=ExitCode.OK)
    assert "rtl: rtl/core/*.sv" in flow.read_text()
    assert _discover(site, "--audit", code=ExitCode.OK)["undeclared"] == []
