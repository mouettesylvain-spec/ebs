"""Tests for `ebs seeds gen` (task P1-11)."""

from __future__ import annotations

import json

from ebs.flow.tables import load_table
from tests.helpers.cli import Site


# R1
def test_gen_appends_rows(site: Site) -> None:
    (site.proj / "reg.csv").write_text("test,seed\nsmoke,1\nlong,2\n")
    result = site.invoke(["seeds", "gen", "reg.csv", "--per-test", "2", "--base-seed", "7"])
    assert result.exit_code == 0, result.output
    assert "4 rows appended, 0 empty seeds filled" in result.stdout
    rows = load_table(site.proj / "reg.csv").rows
    assert [r["test"] for r in rows] == ["smoke", "long", "smoke", "smoke", "long", "long"]


# R2
def test_gen_replace_json(site: Site) -> None:
    path = site.proj / "reg.yaml"
    path.write_text("- {test: smoke, seed: ''}\n")
    result = site.invoke(["seeds", "gen", str(path), "--replace", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "v": 1, "table": str(path), "appended": 0, "filled": 1,
    }  # fmt: skip
    assert load_table(path).rows[0]["seed"] != ""


def test_gen_errors_exit_2(site: Site) -> None:
    (site.proj / "reg.csv").write_text("test,seed\nsmoke,1\n")
    result = site.invoke(["seeds", "gen", "reg.csv"])
    assert result.exit_code == 2
    assert "nothing to do" in result.stderr
    result = site.invoke(["seeds", "gen", "reg.csv", "--per-test", "1", "--column", "s"])
    assert result.exit_code == 2
    assert "no column 's'" in result.stderr
