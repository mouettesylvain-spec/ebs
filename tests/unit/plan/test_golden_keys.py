"""Invariant I4: golden action keys are stable unless KEY_SCHEMA_VERSION is bumped (P0-08 R9)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

from ebs.core.canon import canonical_json
from ebs.plan.keys import KEY_SCHEMA_VERSION, compute_key
from ebs.plan.planfile import spec_from_json, spec_to_json

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "golden_keys"
SCRIPT = ROOT / "scripts" / "regen_golden_keys.py"
FIX = (
    "If the change is intended, bump KEY_SCHEMA_VERSION in src/ebs/plan/keys.py and run "
    "`uv run python scripts/regen_golden_keys.py`, and explain why in the commit body: a new "
    "key schema invalidates every cache entry. Otherwise the change is a regression: fix it."
)


@pytest.fixture(scope="module")
def regen() -> ModuleType:
    spec = importlib.util.spec_from_file_location("regen_golden_keys", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["regen_golden_keys"] = module
    spec.loader.exec_module(module)
    return module


def fixtures() -> list[Path]:
    return sorted(FIXTURES.glob("*.json"))


def test_at_least_five_fixtures() -> None:
    assert len(fixtures()) >= 5


# R9 (I4)
@pytest.mark.parametrize("path", fixtures(), ids=lambda p: p.stem)
def test_keys_stable(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc["key_schema"] != KEY_SCHEMA_VERSION:
        pytest.fail(
            f"{path.name} holds keys of schema {doc['key_schema']} but KEY_SCHEMA_VERSION is "
            f"{KEY_SCHEMA_VERSION}: run `uv run python scripts/regen_golden_keys.py`"
        )
    key = str(compute_key(spec_from_json(doc["spec"])))
    if key != doc["key"]:
        pytest.fail(
            f"the action key of golden spec {path.stem!r} changed ({doc['key']} -> {key}). {FIX}"
        )


def test_fixtures_are_what_the_script_generates(regen: ModuleType) -> None:
    samples = regen.samples()
    assert sorted(samples) == [p.stem for p in fixtures()]
    for path in fixtures():
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert canonical_json(doc["spec"]) == canonical_json(spec_to_json(samples[path.stem]))


# R9
def test_regen_refuses_without_a_schema_bump(
    regen: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "golden"
    shutil.copytree(FIXTURES, target)
    before = {p.name: p.read_bytes() for p in target.iterdir()}
    assert regen.main(["--fixtures", str(target)]) == 1
    assert "KEY_SCHEMA_VERSION" in capsys.readouterr().err
    assert {p.name: p.read_bytes() for p in target.iterdir()} == before


# R9
def test_regen_rewrites_after_a_schema_bump(regen: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "golden"
    shutil.copytree(FIXTURES, target)
    for path in target.iterdir():
        doc = json.loads(path.read_text())
        doc.update(key_schema=KEY_SCHEMA_VERSION - 1, key="sha256:" + "0" * 64)
        path.write_text(json.dumps(doc))
    (target / "stale_sample.json").write_text(json.dumps({"key_schema": 0}))
    assert regen.main(["--fixtures", str(target)]) == 0
    assert sorted(p.name for p in target.iterdir()) == [p.name for p in fixtures()]
    for path in target.iterdir():
        assert path.read_bytes() == (FIXTURES / path.name).read_bytes()


def test_regen_creates_missing_fixtures(regen: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "new"
    assert regen.main(["--fixtures", str(target)]) == 0
    assert sorted(p.name for p in target.iterdir()) == [p.name for p in fixtures()]
