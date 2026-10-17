"""Health probes (R6) and `whoami`; the probes need no credentials."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, current_principal, get_deps
from ebs.meta.service.routes import ok
from ebs.meta.service.tokens import TokenKind

probes = APIRouter(include_in_schema=False)
api = APIRouter(tags=["auth"], dependencies=[Depends(current_principal)])


@probes.get("/healthz")
def healthz() -> Response:
    """The process serves requests."""
    return JSONResponse({"status": "ok"})


@probes.get("/readyz")
def readyz(deps: Annotated[ServiceDeps, Depends(get_deps)]) -> Response:
    """The database is reachable and its schema is at the migrations' head."""
    reason = deps.readiness.check()
    if reason is None:
        return JSONResponse({"status": "ready"})
    return JSONResponse({"status": "not ready", "detail": reason}, status_code=503)


class Whoami(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    via: Literal["munge", "token"]
    token_kind: TokenKind | None
    team: str | None


@api.get("/whoami", response_model=Whoami)
def whoami(
    caller: Annotated[Principal, Depends(current_principal)],
    deps: Annotated[ServiceDeps, Depends(get_deps)],
) -> Response:
    """The authenticated caller, to check credentials (`curl -H "X-EBS-Munge: $(munge -n)"`)."""
    deps.check(caller, "read", domain=None, owner=None)
    return ok(
        Whoami(name=caller.name, via=caller.via, token_kind=caller.token_kind, team=caller.team)
    )
