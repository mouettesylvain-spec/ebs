"""Tests for plan.json encoding (task P0-08 R8)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ebs.cas.fs import FsCAS
from ebs.core.canon import canonical_json, digest_json
from ebs.core.digest import hash_bytes
from ebs.core.errors import PlanError
from ebs.plan.keys import KEY_SCHEMA_VERSION, with_key
from ebs.plan.planfile import (
    decode,
    encode,
    from_json,
    plan_digest,
    spec_from_json,
    spec_to_json,
    to_json,
)
from ebs.plan.types import ActionSpec, DebugSpec, FlowInfo, GitInfo, ImportInput, InputRef, Plan
from tests.helpers.plan import ARCH_STEPS, RTL, arch_flow, planner, resources
from tests.helpers.plan_strategies import specs, texts


@st.composite
def rich_specs(draw: st.DrawFn) -> ActionSpec:
    spec = draw(specs())
    maybe_int = st.none() | st.integers(1, 1 << 40)
    extra = []
    if draw(st.booleans()):
        extra.append(InputRef("zz/imported", "tree", ImportInput("rtl", "**"), None))
    spec = dataclasses.replace(
        spec,
        inputs=(*spec.inputs, *extra),
        resources=resources(draw(maybe_int), draw(maybe_int), draw(maybe_int)),
        debug=DebugSpec(
            tuple(draw(st.lists(texts, max_size=2))), draw(maybe_int), draw(st.booleans())
        ),
        runtime_env=draw(st.dictionaries(texts, texts, max_size=2)),
        key=None,
    )
    return with_key(spec) if draw(st.booleans()) else spec


# R8
@given(rich_specs())
def test_spec_roundtrip(spec: ActionSpec) -> None:
    doc = spec_to_json(spec)
    assert spec_from_json(json.loads(canonical_json(doc))) == spec


# R8
def test_roundtrip(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    plan = p.plan(
        arch_flow(tmp_path),
        base=tmp_path,
        info=FlowInfo("cpu/flow.yaml", GitInfo("git@example.invalid:cpu.git", "a" * 40, True)),
    )
    plan = dataclasses.replace(plan, lock=hash_bytes(b"lock"))
    data = encode(plan)
    assert data == canonical_json(json.loads(data))  # canonical JSON
    assert decode(data) == plan
    assert plan_digest(plan) == digest_json(json.loads(data))
    assert from_json(to_json(plan)) == plan


# R8
def test_roundtrip_with_pending_keys(cas: FsCAS, tmp_path: Path) -> None:
    steps: dict[str, Any] = dict(ARCH_STEPS)
    steps["compile"] = {**ARCH_STEPS["compile"], "outputs": {"worklib": {"dir": "work/${row.lib}"}}}
    p, _ = planner(cas, RTL)
    plan = p.plan(arch_flow(tmp_path, steps), base=tmp_path)
    assert any(a.key is None for a in plan.actions)
    assert decode(encode(plan)) == plan


def test_top_level_layout(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    doc = to_json(p.plan(arch_flow(tmp_path), base=tmp_path))
    assert isinstance(doc, dict)
    assert set(doc) == {
        "v", "ebs_version", "key_schema", "flow", "lock", "domain", "project", "toolchains",
        "actions", "edges",
    }  # fmt: skip
    assert doc["v"] == 1
    assert doc["key_schema"] == KEY_SCHEMA_VERSION
    assert doc["flow"] == {"path": "flow.yaml", "git": None}
    # P0-13: the runner applies the toolchain env, so the plan carries it ($HOME left literal).
    toolchains = doc["toolchains"]
    assert isinstance(toolchains, dict)
    assert toolchains["questa"] == {
        "module": "questa/2025.2",
        "id": doc["actions"][0]["toolchain"]["id"],  # type: ignore[call-overload,index]
        "env": {"TOOL_HOME": "/opt/eda-tools/questa/2025.2"},
    }


def _plan_doc(cas: FsCAS, tmp_path: Path) -> dict[str, Any]:
    p, _ = planner(cas, RTL)
    doc = json.loads(encode(p.plan(arch_flow(tmp_path), base=tmp_path)))
    assert isinstance(doc, dict)
    return doc


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(v=2), r"plan format version 2"),
        (lambda d: d.update(key_schema=KEY_SCHEMA_VERSION + 1), r"key schema"),
        (lambda d: d.pop("domain"), r"domain"),
        (lambda d: d["actions"][0].update(argv="vsim"), r"actions\[0\]\.argv"),
        (lambda d: d["actions"][0].update(key="md5:x"), r"actions\[0\]\.key"),
        (lambda d: d["actions"][0]["inputs"][0]["source"].update(type="bogus"), r"source"),
        (lambda d: d["actions"][0]["inputs"][0].update(kind="dir"), r"kind"),
        (lambda d: d["actions"][0]["params"].update(x=1.5), r"params"),
        (lambda d: d["actions"][0]["resources"].update(cpus="2"), r"resources"),
        (lambda d: d.update(edges=[["a", "b"]]), r"edges"),
        (lambda d: d["toolchains"]["questa"].pop("env"), r"toolchains\.questa.*env"),
        (lambda d: d["toolchains"]["questa"]["env"].update(X=1), r"toolchains\.questa\.env"),
        (lambda d: d["actions"][0]["argv"].append("-x"), r"actions\[0\]\.key does not match"),
        (
            lambda d: d["actions"][0]["inputs"][0].update(id=None),
            r"actions\[0\]\.key does not match",
        ),
    ],
)
def test_decode_rejects_malformed(cas: FsCAS, tmp_path: Path, mutate: Any, message: str) -> None:
    doc = _plan_doc(cas, tmp_path)
    mutate(doc)
    with pytest.raises(PlanError, match=message):
        decode(json.dumps(doc).encode())


def test_decode_rejects_non_json() -> None:
    with pytest.raises(PlanError, match=r"not valid JSON"):
        decode(b"{")
    with pytest.raises(PlanError, match=r"object"):
        decode(b"[]")


def test_plans_are_equal_after_reencoding(cas: FsCAS, tmp_path: Path) -> None:
    p, _ = planner(cas, RTL)
    plan: Plan = p.plan(arch_flow(tmp_path), base=tmp_path)
    assert encode(decode(encode(plan))) == encode(plan)
