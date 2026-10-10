from __future__ import annotations

import json
from typing import Any

from ebs.core.errors import ExitCode
from tests.helpers.cli import Site
from tests.helpers.outputs import ODD, U1, write_outputs_build


def _rows(site: Site, *args: str) -> list[dict[str, Any]]:
    result = site.invoke(["outputs", *args, "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    rows: list[dict[str, Any]] = json.loads(result.stdout)["outputs"]
    return rows


def _keys(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return [(r["action_id"], r["name"]) for r in rows]


# R1
def test_lists_done_cached_and_failed(site: Site) -> None:
    built = write_outputs_build(site)
    rows = _rows(site)
    assert _keys(rows) == [
        ("gen", "extra"),
        ("gen", "model"),
        ("gen", "report"),
        ("sim[test=smoke]", "result"),
        ("sim[test=random]", "result"),
        (ODD, "out"),
        ("gone", "out"),
    ]  # plan order, then output name; `late` has no result yet
    states = {r["action_id"]: r["state"] for r in rows}
    assert states == {
        "gen": "done",
        "sim[test=smoke]": "cached",
        "sim[test=random]": "failed",
        ODD: "done",
        "gone": "done",
    }
    report = built.results["gen"].outputs["report"]
    gen_report = rows[2]
    assert gen_report["digest"] == str(report.digest)
    assert gen_report["size"] == report.size
    assert gen_report["type"] == "file"
    assert rows[1]["type"] == "tree"

    text = site.invoke(["outputs"])
    assert text.exit_code == ExitCode.OK, text.output
    lines = text.stdout.splitlines()
    header = next(line for line in lines if "ACTION" in line)
    for column in ("ACTION", "OUTPUT", "PATH", "TYPE", "SIZE", "DIGEST"):
        assert column in header
    report_line = next(line for line in lines if "reports/gen.rpt" in line)
    assert report_line.split()[:2] == ["gen", "report"]
    assert str(report.digest) in report_line
    assert any("sim[test=random]" in line and "result.json" in line for line in lines)
    assert not any(line.startswith("late") for line in lines)


# R1
def test_filters_by_action_and_step(site: Site) -> None:
    write_outputs_build(site)
    assert _keys(_rows(site, "sim[test=s")) == [("sim[test=smoke]", "result")]  # prefix
    assert _keys(_rows(site, "gen", ODD)) == [
        ("gen", "extra"),
        ("gen", "model"),
        ("gen", "report"),
        (ODD, "out"),
    ]
    assert _keys(_rows(site, "--step", "sim")) == [
        ("sim[test=smoke]", "result"),
        ("sim[test=random]", "result"),
    ]
    assert _rows(site, "gen", "--step", "sim") == []  # both filters apply
    ambiguous = site.invoke(["outputs", "sim"])
    assert ambiguous.exit_code == ExitCode.USAGE
    assert "sim[test=smoke]" in ambiguous.stderr
    unknown = site.invoke(["outputs", "--step", "nope"])
    assert unknown.exit_code == ExitCode.USAGE
    assert "nope" in unknown.stderr


# R1
def test_json_shape(site: Site) -> None:
    built = write_outputs_build(site)
    doc = json.loads(site.invoke(["outputs", "--json", "--build", str(built.build)]).stdout)
    assert list(doc) == ["v", "uuid", "build", "domain", "outputs"]
    assert doc["v"] == 1
    assert doc["uuid"] == str(U1)
    assert doc["build"] == built.build
    assert doc["domain"] == "test"
    model = next(r for r in doc["outputs"] if r["name"] == "model")
    out = built.results["gen"].outputs["model"]
    assert model == {
        "action_id": "gen",
        "step": "gen",
        "state": "done",
        "name": "model",
        "path": "model",
        "type": "tree",
        "size": out.size,
        "digest": str(out.digest),
        "id": str(out.id),
    }


# R2
def test_path_from_final_plan(site: Site) -> None:
    write_outputs_build(site)  # the submitted plan says `submitted/...`, the final one does not
    paths = {(r["action_id"], r["name"]): r["path"] for r in _rows(site)}
    assert paths[("gen", "report")] == "reports/gen.rpt"
    assert paths[("sim[test=random]", "result")] == "result.json"
    assert paths[(ODD, "out")] == "out.txt"


# R2
def test_path_from_plan_without_final_plan(site: Site) -> None:
    write_outputs_build(site, final_plan=False)
    paths = {(r["action_id"], r["name"]): r["path"] for r in _rows(site)}
    assert paths[("gen", "report")] == "reports/gen.rpt"


# R2
def test_output_not_in_plan_listed_with_null_path(site: Site) -> None:
    write_outputs_build(site)
    rows = _rows(site, "gen")
    extra = next(r for r in rows if r["name"] == "extra")
    assert extra["path"] is None
    assert extra["size"] == len(b"not in the plan\n")
    text = site.invoke(["outputs", "gen"]).stdout
    assert any("extra" in line for line in text.splitlines())


# R5
def test_needs_metadata(site: Site) -> None:
    write_outputs_build(site)
    site.services.with_store = False
    result = site.invoke(["outputs"])
    assert result.exit_code == ExitCode.USAGE
    assert "[metadata].url" in result.stderr
