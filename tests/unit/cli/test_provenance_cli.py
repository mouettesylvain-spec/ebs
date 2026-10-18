"""`ebs why`, `ebs used-by` and `ebs tests` (P2-04 R1-R3 through the CLI).

The text and DOT renderings are compared with golden files under `golden/provenance/`; if a
change is intended, regenerate them with `EBS_UPDATE_GOLDEN=1 uv run pytest
tests/unit/cli/test_provenance_cli.py -q` and review the diff.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from ebs.core.clock import FakeClock
from ebs.core.errors import ExitCode
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.provenance import LIB, RES, TC, S, World, world

GOLDEN = Path(__file__).parent / "golden" / "provenance"


def _golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("EBS_UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if not path.exists():
        pytest.fail(f"{path} is missing; create it with EBS_UPDATE_GOLDEN=1")
    assert text == path.read_text()


@pytest.fixture
def site(tmp_path: Path) -> Site:
    write_site(tmp_path)
    return Site(tmp_path, TestServices(tmp_path))


@pytest.fixture
def w(site: Site) -> World:
    clock = site.services.clock
    assert isinstance(clock, FakeClock)
    return world(S(site.services.memory_store, clock))


# R1: the upstream chain as text, JSON and Graphviz.
def test_why_outputs(site: Site, w: World) -> None:
    target = str(RES["t1-1"].id)
    text = site.invoke(["why", target, "--domain", "test"])
    assert text.exit_code == ExitCode.OK, text.output
    _golden("why.txt", text.stdout)
    dot = site.invoke(["why", target, "--domain", "test", "--dot"])
    assert dot.exit_code == ExitCode.OK, dot.output
    assert dot.stdout.startswith("digraph provenance {")
    _golden("why.dot", dot.stdout)
    doc = json.loads(site.invoke(["why", target, "--domain", "test", "--json"]).stdout)
    assert doc["v"] == 1
    assert doc["truncated"] is False
    [graph] = doc["graphs"]
    compile_ = next(a for a in graph["actions"] if a["action_id"] == "compile[lib=core]")
    assert compile_["params"] == {"lib": "core", "vlog_opts": "-sv"}
    assert compile_["toolchain"] == str(TC)
    assert compile_["flow_commit"] == "abc123"
    assert sorted(s["paths"] for s in doc["sources"]) == [["src/a.sv", "srcs/a.sv"], ["srcs/b.sv"]]
    both = site.invoke(["why", target, "--domain", "test", "--json", "--dot"])
    assert both.exit_code == ExitCode.USAGE
    assert "not both" in both.output
    shallow = site.invoke(["why", target, "--domain", "test", "--depth", "1"])
    assert "truncated at depth 1" in shallow.stdout


# R1: the target is a digest, a release (output) or an action of a build.
def test_why_targets(site: Site, w: World) -> None:
    def actions(*args: str) -> set[str]:
        r = site.invoke(["why", *args, "--json"])
        assert r.exit_code == ExitCode.OK, r.output
        return {a["action_id"] for g in json.loads(r.stdout)["graphs"] for a in g["actions"]}

    chain = {"compile[lib=core]", "gen"}
    assert actions(str(LIB.digest), "--domain", "test") == chain
    assert actions("rtl/cpu@1.0", "--domain", "test") == chain
    assert actions("rtl/cpu@1.0:compile[lib=core].worklib", "--domain", "test") == chain
    assert actions("compile[lib=core]", "--build", str(w.rtl)) == chain
    assert actions(str(RES["o"].id), "--domain", "other") == {"sim[seed=7,test=t9]", *chain}
    for bad, message in [
        ("rtl/cpu@9", "no release rtl/cpu 9"),
        ("rtl/cpu@1.0:nope.out", "has no output 'nope.out'"),
    ]:
        r = site.invoke(["why", bad, "--domain", "test"])
        assert r.exit_code == ExitCode.USAGE, r.output
        assert message in r.output
    unknown = site.invoke(["why", "nosuch", "--build", str(w.rtl)])
    assert unknown.exit_code == ExitCode.USAGE
    assert "nosuch" in unknown.output


# R2
def test_used_by(site: Site, w: World) -> None:
    r = site.invoke(["used-by", "rtl/cpu", "1.0", "--domain", "test"])
    assert r.exit_code == ExitCode.OK, r.output
    _golden("used_by.txt", r.stdout)
    doc = json.loads(site.invoke(["used-by", "rtl/cpu", "1.0", "--domain", "test",
                                  "--json"]).stdout)  # fmt: skip
    assert [(b["domain"], b["build"]) for b in doc["builds"]] == [
        ("test", w.verif), ("test", w.soc), ("test", w.rerun), ("other", w.other),
    ]  # fmt: skip
    assert {(x["name"], x["via"]) for x in doc["releases"]} == {
        ("verif/model", "outputs"), ("rtl/cpu", "export"),
    }  # fmt: skip
    missing = site.invoke(["used-by", "rtl/cpu", "9", "--domain", "test"])
    assert missing.exit_code == ExitCode.USAGE


# R3: the runs, --latest, and the JUnit export.
def test_tests(site: Site, w: World, tmp_path: Path) -> None:
    r = site.invoke(["tests", "rtl/cpu", "1.0", "--domain", "test"])
    assert r.exit_code == ExitCode.OK, r.output
    _golden("tests.txt", r.stdout)
    latest = site.invoke(["tests", "rtl/cpu", "1.0", "--domain", "test", "--latest", "--json"])
    rows = json.loads(latest.stdout)["tests"]
    assert [(x["test"], x["seed"], x["build"], x["status"]) for x in rows] == [
        ("t1", 1, w.rerun, "passed"), ("t1", 2, w.verif, "failed"), ("t9", 7, w.other, "passed"),
    ]  # fmt: skip
    junit = tmp_path / "out" / "tests.xml"
    j = site.invoke(["tests", "rtl/cpu", "1.0", "--domain", "test", "--latest",
                     "--junit", str(junit)])  # fmt: skip
    assert j.exit_code == ExitCode.OK, j.output
    suite = ET.parse(junit).getroot()
    assert suite.tag == "testsuite"
    assert (suite.get("name"), suite.get("tests"), suite.get("failures")) == ("rtl/cpu 1.0", "3",
                                                                              "1")  # fmt: skip
    cases = suite.findall("testcase")
    assert [c.get("name") for c in cases] == ["t1 seed=1", "t1 seed=2", "t9 seed=7"]
    assert [c.find("failure") is not None for c in cases] == [False, True, False]
