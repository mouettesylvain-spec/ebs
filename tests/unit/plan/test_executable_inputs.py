"""The executable bit of source files: in the action key, in plan.json, staged by the runner.

A source file's id is its content digest; before KEY_SCHEMA_VERSION 2 its mode was lost, so a
`chmod +x` changed no key and the runner staged every source 0444 (`./run.sh` failed).
"""

from __future__ import annotations

import dataclasses
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.clock import FakeClock
from ebs.core.errors import PlanError
from ebs.plan.diff import diff_plans
from ebs.plan.keys import action_key_document, compute_key
from ebs.plan.planfile import spec_from_json, spec_to_json
from ebs.plan.planner import Planner
from ebs.plan.types import Plan
from ebs.sources.snapshot import SourceSnapshotter
from ebs.sources.statcache import StatCache
from tests.helpers.plan import FakeToolchains, flow, registry

RUN = {
    "kind": "shell",
    "command": ["./tools/run.sh"],
    "inputs": {"tools": "tools/*", "rtl": "rtl/*.sv"},
}


def _sources(base: Path) -> None:
    (base / "tools").mkdir()
    (base / "tools" / "run.sh").write_text("#!/bin/sh\necho ok\n")
    (base / "rtl").mkdir()
    (base / "rtl" / "a.sv").write_text("module a; endmodule\n")


def _plan(cas: FsCAS, base: Path, clock: FakeClock, step: dict[str, Any] = RUN) -> Plan:
    with StatCache(base / "stat.sqlite") as cache:
        sources = SourceSnapshotter(cas, cache, clock=clock, git=None, audit_fraction=0)
        return Planner(cas, sources, FakeToolchains(), registry()).plan(
            flow({"s": step}), base=base
        )


def _executable(plan: Plan) -> dict[str, bool]:
    return {r.logical_path: r.executable for r in plan.action("s").inputs}


def test_source_mode_planned(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    _sources(tmp_path)
    (tmp_path / "tools" / "run.sh").chmod(0o755)
    assert _executable(_plan(cas, tmp_path, fake_clock)) == {
        "rtl/a.sv": False,
        "tools/run.sh": True,
    }


# A chmod-only change reruns the action, and `plan --diff` names the file.
def test_chmod_changes_key(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    _sources(tmp_path)
    before = _plan(cas, tmp_path, fake_clock)
    (tmp_path / "tools" / "run.sh").chmod(0o755)
    fake_clock.advance(3600)  # past the racy window: the stat cache is consulted, mode and all
    after = _plan(cas, tmp_path, fake_clock)
    assert before.action("s").key != after.action("s").key
    (diff,) = diff_plans(before, after)
    assert (diff.status, diff.changed) == ("changed", ('executable["tools/run.sh"]',))
    (tmp_path / "tools" / "run.sh").chmod(0o644)
    assert _plan(cas, tmp_path, fake_clock).action("s").key == before.action("s").key


def test_key_document_lists_executable_paths(
    cas: FsCAS, tmp_path: Path, fake_clock: FakeClock
) -> None:
    _sources(tmp_path)
    (tmp_path / "tools" / "run.sh").chmod(0o700)  # owner bit alone counts
    doc = action_key_document(_plan(cas, tmp_path, fake_clock).action("s"))
    assert isinstance(doc, dict)
    assert doc["schema"] == 2
    assert doc["executable"] == {"tools/run.sh": True}


# The same file reached by two patterns is one input; it cannot be both executable and not.
def test_overlapping_patterns_agree(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    _sources(tmp_path)
    (tmp_path / "tools" / "run.sh").chmod(0o755)
    step = {**RUN, "inputs": {"a": "tools/*", "b": "tools/run.sh"}}
    assert _executable(_plan(cas, tmp_path, fake_clock, step)) == {"tools/run.sh": True}


def test_planfile_roundtrip(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    _sources(tmp_path)
    (tmp_path / "tools" / "run.sh").chmod(0o755)
    spec = _plan(cas, tmp_path, fake_clock).action("s")
    doc = json.loads(json.dumps(spec_to_json(spec)))
    plain, script = doc["inputs"]
    assert "executable" not in plain  # only when set
    assert script["executable"] is True
    assert spec_from_json(doc) == spec
    script["executable"] = 1
    with pytest.raises(PlanError, match="executable"):
        spec_from_json(doc)
    flipped = dataclasses.replace(
        spec, inputs=tuple(dataclasses.replace(r, executable=False) for r in spec.inputs)
    )
    assert compute_key(flipped) != spec.key


def test_mode_bits_other_than_x_ignored(cas: FsCAS, tmp_path: Path, fake_clock: FakeClock) -> None:
    _sources(tmp_path)
    key = _plan(cas, tmp_path, fake_clock).action("s").key
    (tmp_path / "rtl" / "a.sv").chmod(0o600)
    fake_clock.advance(3600)
    assert _plan(cas, tmp_path, fake_clock).action("s").key == key
    assert stat.S_IMODE((tmp_path / "rtl" / "a.sv").stat().st_mode) == 0o600
