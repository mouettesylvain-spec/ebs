"""JSON Schema of `flow.yaml`, generated from the Pydantic models (task P0-04 R9).

The committed copy lives in `schemas/flow.v1.schema.json` (regenerate with
`uv run python scripts/gen_schema.py`) so editors can offer completion and validation. The schema
checks structure, names and literal formats; `load_flow` additionally checks `${…}` syntax, output
paths and cross-references, which JSON Schema cannot express.
"""

from __future__ import annotations

import json

from ebs.core.types import JsonValue
from ebs.flow.model import Flow

__all__ = ["JSON_SCHEMA_DIALECT", "SCHEMA_ID", "flow_json_schema", "render_schema"]

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID = "urn:ebs:schema:flow:v1"


def flow_json_schema() -> JsonValue:
    """The JSON Schema (draft 2020-12) of the flow file format, version 1."""
    generated = Flow.model_json_schema(mode="validation")
    schema: JsonValue = json.loads(
        json.dumps({"$schema": JSON_SCHEMA_DIALECT, "$id": SCHEMA_ID, **generated})
    )
    _close_pattern_properties(schema)
    return schema


def _close_pattern_properties(node: JsonValue) -> None:
    # Pydantic renders constrained dict keys (step/output names, ...) as `patternProperties`,
    # which alone still admits keys that match no pattern; the models reject those.
    if isinstance(node, dict):
        if "patternProperties" in node:
            node.setdefault("additionalProperties", False)
        for value in node.values():
            _close_pattern_properties(value)
    elif isinstance(node, list):
        for value in node:
            _close_pattern_properties(value)


def render_schema() -> str:
    """`flow_json_schema()` as the exact text committed in `schemas/flow.v1.schema.json`."""
    return json.dumps(flow_json_schema(), indent=2, ensure_ascii=False) + "\n"
