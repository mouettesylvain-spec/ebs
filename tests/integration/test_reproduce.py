"""`ebs reproduce --run` of a passed action gives the outputs the runner recorded (P1-10 R3)."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from ebs.core.digest import hash_file
from ebs.core.errors import ExitCode
from ebs.core.tree import build_tree
from ebs.meta.api import BuildId
from tests.helpers.cli import Site, TestServices, write_site
from tests.helpers.repro import GEN, write_repro_build, write_repro_site


def _site(tmp_path: Path) -> Site:
    write_site(tmp_path)
    site = Site(tmp_path, TestServices(tmp_path))
    write_repro_site(site, copy_threshold=8)
    return site


def _run(site: Site, dest: Path) -> dict[str, Any]:
    result = site.invoke(["reproduce", GEN, "--dir", str(dest), "--run", "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


# R3
def test_identical_outputs(tmp_path: Path) -> None:
    site = _site(tmp_path)
    built = write_repro_build(site)
    recorded = built.results[GEN].outputs
    dest = tmp_path / "repro"
    doc = _run(site, dest)
    assert doc["ran"]["exit_code"] == 0
    assert doc["ran"]["status"] == "passed"
    by_name = {o["name"]: o for o in doc["ran"]["outputs"]}
    assert by_name["out"] == {
        "name": "out",
        "status": "same",
        "recorded": str(recorded["out"].digest),
        "reproduced": str(recorded["out"].digest),
    }
    # Compared independently of reproduce's own report: the bytes on disk hash to the record.
    assert hash_file(dest / "work" / "out.txt", "sha256")[0] == recorded["out"].digest
    model = build_tree(dest / "work" / "model", partial(hash_file, algo="sha256"))[0]
    assert by_name["model"]["status"] == "not_compared"  # deterministic: false
    assert by_name["model"]["reproduced"] == str(model)
    assert doc["ran"]["identical"] is True


# R3: a difference is reported, never hidden (the control for the test above).
def test_different_output_reported(tmp_path: Path) -> None:
    site = _site(tmp_path)
    built = write_repro_build(site)
    gen = built.results[GEN]
    wrong = gen.outputs["out"].model_copy(update={"digest": built.cas.put_bytes(b"other\n")})
    store = site.services.memory_store
    store.record_result(
        BuildId(built.build), GEN, gen.model_copy(update={"outputs": {**gen.outputs, "out": wrong}})
    )
    doc = _run(site, tmp_path / "repro")
    out = next(o for o in doc["ran"]["outputs"] if o["name"] == "out")
    assert out["status"] == "different"
    assert out["recorded"] == str(wrong.digest)
    assert doc["ran"]["identical"] is False
    text = site.invoke(["reproduce", GEN, "--dir", str(tmp_path / "again"), "--run"])
    assert text.exit_code == ExitCode.OK, text.output
    assert "differs" in text.stdout
