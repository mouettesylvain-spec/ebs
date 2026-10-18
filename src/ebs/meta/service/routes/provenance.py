"""Provenance walks (P2-04): `POST /domains/{domain}/provenance/{upstream,downstream}`.

Each route walks one domain and is authorized as a `read` of it (403 for non-members, R4).
Crossing domains is the client's job (`ebs.release.provenance`), through export audit rows and
release reads that the release routes authorize on their own.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.meta.api import ProvenanceGraph, ProvenanceQuery, check_domain
from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import ok

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(tags=["provenance"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]
Query = Annotated[ProvenanceQuery, Depends(body(ProvenanceQuery))]


@router.post(
    "/domains/{domain}/provenance/upstream",
    response_model=ProvenanceGraph,
    openapi_extra=json_body(ProvenanceQuery),
)
def upstream(domain: str, query: Query, deps: Deps, caller: Caller) -> Response:
    deps.check(caller, "read", domain=check_domain(domain), owner=None)
    return ok(
        deps.store.provenance_upstream(domain, ids=query.ids, keys=query.keys, depth=query.depth)
    )


@router.post(
    "/domains/{domain}/provenance/downstream",
    response_model=ProvenanceGraph,
    openapi_extra=json_body(ProvenanceQuery),
)
def downstream(domain: str, query: Query, deps: Deps, caller: Caller) -> Response:
    deps.check(caller, "read", domain=check_domain(domain), owner=None)
    return ok(
        deps.store.provenance_downstream(domain, ids=query.ids, keys=query.keys, depth=query.depth)
    )
