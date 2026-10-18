"""The dashboard's API types are generated from web/openapi.json (P2-06): it must match the
service's routes and models, so a route or model change regenerates them in the same commit."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.dump_openapi import openapi

ROOT = Path(__file__).resolve().parents[4]


# R7: the dashboard calls exactly the routes (and authz-scoped models) the service serves.
def test_web_openapi_is_current() -> None:
    committed = json.loads((ROOT / "web" / "openapi.json").read_text(encoding="utf-8"))
    current = json.loads(json.dumps(openapi(), sort_keys=True))
    for doc in (committed, current):
        doc["info"].pop("version", None)  # the package version, not the API
    assert committed == current, (
        "web/openapi.json is stale: run `cd web && npm run gen:api` and commit openapi.json "
        "and src/api/schema.d.ts"
    )
