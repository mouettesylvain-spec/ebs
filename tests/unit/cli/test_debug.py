"""`ebs debug ACTION` (P1-10 R1): everything needed to understand one action, from a login node."""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from ebs.core.errors import ExitCode
from ebs.meta.api import BuildId
from ebs.runner.collect import debug_action_dir
from tests.helpers.cli import Site
from tests.helpers.repro import (
    FIRST_ERROR,
    GEN,
    JOB,
    NODE,
    SIGNATURE,
    SIM,
    TOOLCHAIN,
    ReproBuild,
    write_repro_build,
    write_repro_site,
)


@pytest.fixture
def built(site: Site) -> ReproBuild:
    write_repro_site(site)
    built = write_repro_build(site)
    # A rule that recognises errors (questa.sim) puts them in the summary; shell has none.
    store = site.services.memory_store
    result = built.results[SIM]
    summary = {**result.summary, "first_error": FIRST_ERROR, "signature": SIGNATURE}
    store.record_result(BuildId(built.build), SIM, result.model_copy(update={"summary": summary}))
    return built


def _doc(site: Site, *args: str) -> dict[str, Any]:
    result = site.invoke(["debug", *args, "--json"])
    assert result.exit_code == ExitCode.OK, result.output
    doc: dict[str, Any] = json.loads(result.stdout)
    return doc


# R1
def test_output_fields(site: Site, built: ReproBuild) -> None:
    result = site.invoke(["debug", "sim[test=s"])  # a unique prefix
    assert result.exit_code == ExitCode.OK, result.output
    text = result.stdout
    spec = built.spec(SIM)
    sim_result = built.results[SIM]
    assert SIM in text
    assert str(built.uuid) in text
    assert "failed" in text
    assert "exit code 1" in text  # the reason
    assert SIGNATURE in text  # first error signature
    assert FIRST_ERROR in text
    assert shlex.join(spec.argv) in text  # argv, shell-quoted
    assert "SEED=7" in text  # declared env
    assert "LM_LICENSE_FILE" not in text  # only the declared env
    model = next(r for r in spec.inputs if r.logical_path == "model")
    out = next(r for r in spec.inputs if r.logical_path == "out.txt")
    assert out.id is not None
    assert model.id is not None
    for ref in (model, out):
        line = next(line for line in text.splitlines() if line.strip().startswith(ref.logical_path))
        assert str(ref.id) in line
    assert "gen.out" in text  # origin: the producer action and output
    assert "gen.model" in text
    content = built.results[GEN].outputs["model"].digest  # the nondeterministic input's bytes
    assert str(content) in text
    assert JOB in text  # SLURM job id
    assert NODE in text  # node
    assert sim_result.runner.host == NODE
    debug_dir = debug_action_dir(built.debug_root, "test", str(built.build), SIM)
    assert (debug_dir / "command.json").is_file()
    assert str(debug_dir) in text


# R1
def test_toolchain_and_source_origin(site: Site, built: ReproBuild) -> None:
    result = site.invoke(["debug", GEN, "--build", str(built.build)])
    assert result.exit_code == ExitCode.OK, result.output
    text = result.stdout
    assert TOOLCHAIN.module in text
    assert str(TOOLCHAIN.id) in text
    assert "FOO=bar" in text
    src = built.spec(GEN).inputs[0]
    line = next(line for line in text.splitlines() if line.strip().startswith("src/a.txt"))
    assert str(src.id) in line
    assert "source" in line
    assert "**" in line  # the glob that matched it
    assert "done" in text


# R1
def test_json(site: Site, built: ReproBuild) -> None:
    doc = _doc(site, SIM)
    spec = built.spec(SIM)
    assert doc["v"] == 1
    assert doc["uuid"] == str(built.uuid)
    assert doc["build"] == built.build
    assert doc["action_id"] == SIM
    assert doc["step"] == "sim"
    assert doc["state"] == "failed"
    assert "exit code 1" in doc["reason"]
    assert doc["exit_code"] == 1
    assert doc["first_error"] == FIRST_ERROR
    assert doc["signature"] == SIGNATURE
    assert doc["key"] == str(spec.key)
    assert doc["argv"] == list(spec.argv)
    assert doc["command"] == shlex.join(spec.argv)
    assert doc["env"] == {"SEED": "7"}
    assert doc["toolchain"] is None
    gen_out = built.results[GEN].outputs
    assert doc["inputs"] == [
        {
            "path": "model",
            "kind": "tree",
            "id": str(gen_out["model"].id),
            "content": str(gen_out["model"].digest),
            "origin": {"type": "action", "action_id": GEN, "output": "model"},
        },
        {
            "path": "out.txt",
            "kind": "file",
            "id": str(gen_out["out"].id),
            "content": str(gen_out["out"].digest),
            "origin": {"type": "action", "action_id": GEN, "output": "out"},
        },
    ]
    assert doc["slurm"] == {"job_id": JOB, "node": NODE}
    assert doc["attempts"] == [{"attempt": 1, "job_id": JOB, "node": None, "reason": None}]
    expected = debug_action_dir(built.debug_root, "test", str(built.build), SIM)
    assert doc["debug_dir"] == str(expected)
    assert doc["log"] == str(built.results[SIM].log)

    gen = _doc(site, GEN)
    assert gen["toolchain"] == {"name": "tc", "module": TOOLCHAIN.module, "id": str(TOOLCHAIN.id)}
    assert gen["inputs"][0]["origin"] == {"type": "source", "input": "srcs", "pattern": "**"}
    assert gen["state"] == "done"
    assert gen["reason"] is None
    assert gen["debug_dir"] is None  # a passed action keeps no debug files


# R1
def test_unknown_action_and_no_store(site: Site, built: ReproBuild, tmp_path: Path) -> None:
    missing = site.invoke(["debug", "nope"])
    assert missing.exit_code == ExitCode.USAGE
    assert "no action 'nope'" in missing.stderr
    site.services.with_store = False  # the config has no [metadata].url
    doc = _doc(site, SIM)  # still useful: plan, events and the debug dir need no store
    assert doc["state"] == "failed"
    assert doc["argv"] == list(built.spec(SIM).argv)
    assert doc["first_error"] is None
    assert doc["slurm"] == {"job_id": JOB, "node": None}
    assert doc["inputs"][0]["content"] is None  # resolving it needs the store
