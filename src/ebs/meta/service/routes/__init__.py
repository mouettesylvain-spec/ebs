"""REST routes of the metadata service (P1-06 R2), mounted under `API_PREFIX` by `app.py`."""

from __future__ import annotations

from fastapi import Response
from pydantic import BaseModel

__all__ = ["no_content", "ok"]


def ok(model: BaseModel) -> Response:
    """A 200 response with `model` serialized as the client parses it (`model_validate_json`)."""
    return Response(content=model.model_dump_json(), media_type="application/json")


def no_content() -> Response:
    return Response(status_code=204)
