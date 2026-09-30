"""Tests for `ebs.flow.schema` and the committed JSON Schema (task P0-04 R9)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest
from ruamel.yaml import YAML

from ebs.core.types import JsonValue
from ebs.flow.schema import SCHEMA_ID, flow_json_schema, render_schema

ROOT = Path(__file__).resolve().parents[3]
COMMITTED = ROOT / "schemas" / "flow.v1.schema.json"
FIXTURES = ROOT / "tests" / "fixtures" / "flows"
VALID = sorted((FIXTURES / "valid").glob("*.yaml"))
INVALID = sorted((FIXTURES / "invalid").glob("*.yaml"))
SCHEMA_RE = re.compile(r"^# schema: (reject|accept|n/a)$")


def _verdict(path: Path) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    m = SCHEMA_RE.match(lines[1]) if len(lines) > 1 else None
    assert m, f"{path.name}: second line must be '# schema: reject|accept|n/a'"
    return m[1]


def _load_plain(path: Path) -> JsonValue:
    data: JsonValue = json.loads(json.dumps(YAML(typ="safe", pure=True).load(path)))
    return data


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(COMMITTED.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


# R9
def test_committed_schema_up_to_date() -> None:
    assert COMMITTED.read_text(encoding="utf-8") == render_schema(), (
        "schemas/flow.v1.schema.json is stale: run `uv run python scripts/gen_schema.py`"
    )
    assert json.loads(render_schema()) == flow_json_schema()


# R9
def test_schema_metadata() -> None:
    schema = flow_json_schema()
    assert isinstance(schema, dict)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"] == SCHEMA_ID
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"version", "project", "domain"}  # type: ignore[arg-type]


# R9
def test_schema_is_deterministic() -> None:
    assert render_schema() == render_schema()
    assert render_schema().endswith("}\n")


# R9
@pytest.mark.parametrize("path", VALID, ids=[p.stem for p in VALID])
def test_valid_fixtures_vs_schema(path: Path) -> None:
    errors = list(_validator().iter_errors(_load_plain(path)))
    assert not errors, [e.message for e in errors]


INVALID_CHECKED = [p for p in INVALID if _verdict(p) != "n/a"]


# R9
@pytest.mark.parametrize("path", INVALID_CHECKED, ids=[p.stem for p in INVALID_CHECKED])
def test_fixtures_vs_schema(path: Path) -> None:
    errors = list(_validator().iter_errors(_load_plain(path)))
    if _verdict(path) == "reject":
        assert errors, f"{path.name} should be rejected by the JSON Schema"
    else:
        # Only the loader's semantic checks (refs, paths, cross-references) catch these.
        assert not errors, [e.message for e in errors]


# R9
def test_enough_schema_rejections() -> None:
    assert sum(_verdict(p) == "reject" for p in INVALID) >= 12


def _gen_schema_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_schema", ROOT / "scripts" / "gen_schema.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# R9
def test_gen_schema_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main = _gen_schema_module().main
    out = tmp_path / "schema.json"
    assert main(["--check", "--output", str(out)]) == 1  # missing counts as stale
    assert "stale" in capsys.readouterr().err
    out.write_text("{}\n", encoding="utf-8")
    assert main(["--check", "--output", str(out)]) == 1
    assert main(["--output", str(out)]) == 0
    assert out.read_text(encoding="utf-8") == render_schema()
    assert main(["--check", "--output", str(out)]) == 0
    assert main(["--check"]) == 0  # the committed file (default path) is current
