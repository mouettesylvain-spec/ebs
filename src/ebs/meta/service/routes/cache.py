"""Action cache, outputs and access tracking routes, scoped by domain (I13)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, MetadataError
from ebs.meta.api import (
    CacheHits,
    Changed,
    DigestBatch,
    OptionalDigest,
    OptionalResult,
    ResultManifest,
)
from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import no_content, ok

router = APIRouter(prefix="/domains/{domain}", tags=["cache"])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _digest(value: str, what: str) -> Digest:
    try:
        return Digest.parse(value)
    except DigestError as exc:
        raise MetadataError(f"{what}: {exc}") from None


@router.get("/cache/{key}", response_model=OptionalResult)
def cache_get(domain: str, key: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "read", domain=domain, owner=None)
    found = deps.store.cache_get(domain, _digest(key, "cache_get key"))
    return ok(OptionalResult(result=found))


@router.post("/cache/lookup", response_model=CacheHits, openapi_extra=json_body(DigestBatch))
def cache_get_many(
    domain: str,
    batch: Annotated[DigestBatch, Depends(body(DigestBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.authorize(caller, "read", domain=domain, owner=None)
    hits: dict[str, ResultManifest] = {}
    for key in dict.fromkeys(batch.digests):
        found = deps.store.cache_get(domain, key)
        if found is not None:
            hits[str(key)] = found
    return ok(CacheHits(results=hits))


@router.put("/cache/{key}", response_model=Changed, openapi_extra=json_body(ResultManifest))
def cache_put(
    domain: str,
    key: str,
    result: Annotated[ResultManifest, Depends(body(ResultManifest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.authorize(caller, "cache_write", domain=domain, owner=None)
    inserted = deps.store.cache_put(domain, _digest(key, "cache_put key"), result)
    return ok(Changed(changed=inserted))


@router.post(
    "/cache/{key}/replace-failed",
    response_model=Changed,
    openapi_extra=json_body(ResultManifest),
)
def cache_replace_failed(
    domain: str,
    key: str,
    result: Annotated[ResultManifest, Depends(body(ResultManifest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.authorize(caller, "cache_write", domain=domain, owner=None)
    replaced = deps.store.cache_replace_failed(
        domain, _digest(key, "cache_replace_failed key"), result
    )
    return ok(Changed(changed=replaced))


@router.get("/outputs/{object_id}", response_model=OptionalDigest)
def resolve_output(domain: str, object_id: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "read", domain=domain, owner=None)
    found = deps.store.resolve_output(domain, _digest(object_id, "resolve_output object_id"))
    return ok(OptionalDigest(digest=found))


@router.post("/touch", status_code=204, openapi_extra=json_body(DigestBatch))
def touch(
    domain: str,
    batch: Annotated[DigestBatch, Depends(body(DigestBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.authorize(caller, "cache_write", domain=domain, owner=None)
    deps.store.touch(domain, batch.digests)
    return no_content()
