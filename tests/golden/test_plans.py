"""Golden plan.json of the architecture.md example flow (task P0-08 R8).

The plan is planned with fake sources/toolchains and compared, byte for byte after
canonicalization, with `plans/architecture_example.json`. A difference means plan.json or
action keys changed: if that is intended, regenerate with
`EBS_UPDATE_GOLDEN=1 uv run pytest tests/golden -q` and give the reason in the commit body
(a key change also needs a KEY_SCHEMA_VERSION bump, docs/design/invariants.md I4).
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json
from ebs.plan.planfile import encode, plan_digest
from tests.helpers.plan import RTL, arch_flow, planner

GOLDEN = Path(__file__).parent / "plans" / "architecture_example.json"


# R8
def test_architecture_example_plan(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path / "flow"), base=tmp_path / "flow")
    plan = dataclasses.replace(plan, ebs_version="golden")  # the package version is not pinned
    data = encode(plan)
    if os.environ.get("EBS_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(json.loads(data), indent=1, ensure_ascii=False) + "\n")
        GOLDEN.with_suffix(".digest").write_text(f"{plan_digest(plan)}\n")
    if not GOLDEN.exists():
        pytest.fail(f"{GOLDEN} is missing; create it with EBS_UPDATE_GOLDEN=1")
    expected = canonical_json(json.loads(GOLDEN.read_text(encoding="utf-8")))
    assert data == expected, (
        "plan.json of the architecture example changed; see this module's docstring"
    )
    # Pinned so that a change of canonical JSON itself is caught too.
    assert str(plan_digest(plan)) == (GOLDEN.with_suffix(".digest").read_text().strip())
