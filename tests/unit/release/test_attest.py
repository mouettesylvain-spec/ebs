"""SLSA v1 provenance statements for releases (P3-03 R1)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from ebs.cas.fs import FsCAS
from ebs.core.canon import digest_json
from ebs.core.digest import hash_bytes
from ebs.core.errors import ReleaseError
from ebs.plan.types import FlowInfo, GitInfo, PlanToolchain
from ebs.release.attest import (
    BUILD_TYPE,
    PREDICATE_TYPE,
    STATEMENT_TYPE,
    ImportDep,
    import_deps,
    params_digest,
    provenance_statement,
)
from ebs.release.lock import release_digest
from tests.helpers.attest import COMMIT, LOCK, QUESTA_ID, Released, released, tree_digest
from tests.helpers.plan import sample_spec

SCHEMAS = Path(__file__).parents[2] / "fixtures" / "attest"


def _schema(name: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((SCHEMAS / name).read_text())
    return loaded


@pytest.fixture
def rel(cas: FsCAS, tmp_path: Path) -> Released:
    return released(cas, tmp_path / "work")


def _statement(rel: Released) -> Any:
    return provenance_statement(
        rel.release, rel.build, rel.plan, import_deps(rel.store, rel.build.id)
    )


# R1: the statement validates against the in-toto Statement v1 and SLSA provenance v1 schemas.
def test_statement_schema(rel: Released) -> None:
    doc = _statement(rel)
    checker = jsonschema.Draft202012Validator
    checker(
        _schema("in-toto-statement-v1.schema.json"), format_checker=checker.FORMAT_CHECKER
    ).validate(doc)
    checker(
        _schema("slsa-provenance-v1.schema.json"), format_checker=checker.FORMAT_CHECKER
    ).validate(doc["predicate"])
    assert doc["_type"] == STATEMENT_TYPE == "https://in-toto.io/Statement/v1"
    assert doc["predicateType"] == PREDICATE_TYPE == "https://slsa.dev/provenance/v1"
    # Plain JSON data: it survives a round trip unchanged.
    assert json.loads(json.dumps(doc)) == doc


# R1: subjects are the released outputs with their content digests.
def test_subjects_are_released_outputs(rel: Released) -> None:
    doc = _statement(rel)
    subjects = doc["subject"]
    assert isinstance(subjects, list)
    by_name = {s["name"]: s for s in subjects}
    assert list(by_name) == ["lint.report", "pack.srcs"]  # sorted
    report = rel.release.outputs["lint.report"]
    assert by_name["lint.report"]["digest"] == {"sha256": report.digest.hex}
    assert by_name["pack.srcs"]["digest"] == {"sha256": tree_digest(rel.tree).hex}
    srcs = rel.release.outputs["pack.srcs"]
    assert by_name["pack.srcs"]["annotations"] == {
        "id": str(srcs.id),
        "type": "tree",
        "size": srcs.size,
        "action": "pack",
        "output": "srcs",
    }


# R1: buildDefinition and runDetails carry the flow, params, lock, imports, toolchains, builder.
def test_statement_fields(rel: Released) -> None:
    doc = _statement(rel)
    predicate = doc["predicate"]
    assert isinstance(predicate, dict)
    definition = predicate["buildDefinition"]
    assert definition["buildType"] == BUILD_TYPE
    ext = definition["externalParameters"]
    assert ext["flow"] == {
        "path": "flows/cpu/flow.yaml",
        "repository": "git@example.invalid:cpu.git",
        "commit": COMMIT,
        "dirty": False,
    }
    assert ext["params"] == str(params_digest(rel.plan))
    assert ext["lock"] == str(LOCK)
    assert ext["project"] == "demo"
    assert ext["release"] == {"name": "verif/cpu", "version": "2026.10.1", "domain": "test"}
    assert definition["internalParameters"]["plan"] == str(rel.build.plan_digest)
    deps = {d["name"]: d for d in definition["resolvedDependencies"]}
    assert deps["imports.rtl"]["uri"] == "ebs:release/rtl-team/cpu@1"
    assert deps["imports.rtl"]["digest"] == {"sha256": release_digest(rel.upstream).hex}
    assert deps["toolchains.questa"] == {
        "name": "toolchains.questa",
        "uri": "ebs:toolchain/questa/2025.2",
        "digest": {"sha256": QUESTA_ID.hex},
    }
    assert deps["flow"]["digest"] == {"gitCommit": COMMIT}
    run = predicate["runDetails"]
    assert run["builder"] == {"id": "ebs@1.2.3"}
    meta = run["metadata"]
    assert meta["invocationId"] == str(rel.build.uuid)
    assert meta["startedOn"] == rel.build.created_at.isoformat().replace("+00:00", "Z")
    assert rel.build.finished_at is not None
    assert meta["finishedOn"] == rel.build.finished_at.isoformat().replace("+00:00", "Z")


# R1: the params digest covers every action's params (and nothing else).
def test_params_digest(rel: Released) -> None:
    plan = rel.plan
    assert params_digest(plan) == digest_json(
        {"lint": {"top": "cpu", "n": 2}, "pack": {"opt": "-O2"}}
    )
    lint, pack = plan.actions
    changed = replace(plan, actions=(lint, replace(pack, params={"opt": "-O3"})))
    assert params_digest(changed) != params_digest(plan)
    reordered = replace(plan, actions=(pack, lint))
    assert params_digest(reordered) == params_digest(plan)
    argv_only = replace(plan, actions=(replace(lint, argv=("other",)), pack))
    assert params_digest(argv_only) == params_digest(plan)


# R1: a plan without lock or git info leaves those parameters out; the build of another plan
# is refused.
def test_statement_without_lock_or_git(rel: Released) -> None:
    from ebs.plan.types import FlowInfo

    plan = replace(
        rel.plan, lock=None, flow=FlowInfo("flow.yaml"), actions=(sample_spec(domain="test"),)
    )
    doc: Any = provenance_statement(rel.release, rel.build, plan, [])
    ext = doc["predicate"]["buildDefinition"]["externalParameters"]
    assert "lock" not in ext
    assert ext["flow"] == {"path": "flow.yaml"}
    names = [d["name"] for d in doc["predicate"]["buildDefinition"]["resolvedDependencies"]]
    assert names == ["toolchains.questa"]
    other = rel.release.model_copy(update={"build": rel.release.build + 1})
    with pytest.raises(ReleaseError, match="was not made from build"):
        provenance_statement(other, rel.build, plan, [])


# R1: the plan that ran (the final plan) is the one recorded.
def test_internal_plan_is_final_plan(rel: Released) -> None:
    final = hash_bytes(b"final plan")
    build = rel.build.model_copy(update={"final_plan_digest": final})
    doc: Any = provenance_statement(rel.release, build, rel.plan, [])
    assert doc["predicate"]["buildDefinition"]["internalParameters"]["plan"] == str(final)


# R1: an import that cannot be read fails instead of vanishing from resolvedDependencies.
def test_import_deps_unreadable_release(rel: Released, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rel.store, "get_release_by_id", lambda _id: None)
    with pytest.raises(ReleaseError, match="through 'rtl'"):
        import_deps(rel.store, rel.build.id)


# R1: dependencies are sorted, times are UTC, dirty and missing fields are recorded faithfully.
def test_statement_edge_cases(rel: Released) -> None:
    tz = timezone(timedelta(hours=2))
    build = rel.build.model_copy(
        update={"created_at": datetime(2026, 10, 1, 12, 0, tzinfo=tz), "finished_at": None}
    )
    plan = replace(
        rel.plan,
        toolchains={
            "zz": PlanToolchain("zz/1", hash_bytes(b"zz")),
            "aa": PlanToolchain("aa/1", hash_bytes(b"aa")),
        },
        flow=FlowInfo("flow.yaml", GitInfo("", COMMIT, True)),
    )
    imports = [
        ImportDep("zlib", "z/lib", "1", hash_bytes(b"z")),
        ImportDep("alib", "a/lib", "2", hash_bytes(b"a")),
    ]
    doc: Any = provenance_statement(rel.release, build, plan, imports)
    definition = doc["predicate"]["buildDefinition"]
    assert [d["name"] for d in definition["resolvedDependencies"]] == [
        "imports.alib", "imports.zlib", "toolchains.aa", "toolchains.zz", "flow",
    ]  # fmt: skip
    assert definition["resolvedDependencies"][-1]["uri"] == "flow.yaml"  # no repository
    assert definition["externalParameters"]["flow"]["dirty"] is True
    meta = doc["predicate"]["runDetails"]["metadata"]
    assert meta["startedOn"] == "2026-10-01T10:00:00Z"
    assert "finishedOn" not in meta
    no_commit = replace(plan, flow=FlowInfo("flow.yaml", GitInfo("r", "", False)))
    doc = provenance_statement(rel.release, build, no_commit, [])
    names = [d["name"] for d in doc["predicate"]["buildDefinition"]["resolvedDependencies"]]
    assert "flow" not in names
