"""Action cache, outputs and access tracking routes, scoped by domain (I13).

Reads outside the caller's domains answer "miss", never 403, so they reveal nothing (I20)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, MetadataError
from ebs.meta.api import (
    BlobBatch,
    CacheHits,
    Changed,
    DigestBatch,
    OptionalDigest,
    OptionalResult,
    ResultManifest,
)
from ebs.meta.service.auth import Principal
from ebs.meta.service.authz import DomainDenied
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import no_content, ok

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(
    prefix="/domains/{domain}", tags=["cache"], dependencies=[Depends(current_principal)]
)

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _member(deps: ServiceDeps, caller: Principal, domain: str) -> bool:
    """Authorize a cache read; outside the caller's domains every lookup is a miss (R2)."""
    try:
        deps.check(caller, "read", domain=domain, owner=None)
    except DomainDenied:
        caller.authorized.append("read:miss")
        return False
    return True


def _digest(value: str, what: str) -> Digest:
    try:
        return Digest.parse(value)
    except DigestError as exc:
        raise MetadataError(f"{what}: {exc}") from None


@router.get("/cache/{key}", response_model=OptionalResult)
def cache_get(domain: str, key: str, deps: Deps, caller: Caller, peek: bool = False) -> Response:
    """`peek=true`: `cache_peek` (a prediction such as `ebs plan`): no hit is counted, in the
    store or in the metrics."""
    digest = _digest(key, "cache_get key")
    if not _member(deps, caller, domain):
        found = None
    elif peek:
        return ok(OptionalResult(result=deps.store.cache_peek(domain, digest)))
    else:
        found = deps.store.cache_get(domain, digest)
    if not peek:
        deps.metrics.count_cache(hits=int(found is not None), misses=int(found is None))
    return ok(OptionalResult(result=found))


@router.post("/cache/lookup", response_model=CacheHits, openapi_extra=json_body(DigestBatch))
def cache_get_many(
    domain: str,
    batch: Annotated[DigestBatch, Depends(body(DigestBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    hits: dict[str, ResultManifest] = {}
    keys = list(dict.fromkeys(batch.digests))
    if _member(deps, caller, domain):
        for key in keys:
            found = deps.store.cache_get(domain, key)
            if found is not None:
                hits[str(key)] = found
    deps.metrics.count_cache(hits=len(hits), misses=len(keys) - len(hits))
    return ok(CacheHits(results=hits))


@router.put("/cache/{key}", response_model=Changed, openapi_extra=json_body(ResultManifest))
def cache_put(
    domain: str,
    key: str,
    result: Annotated[ResultManifest, Depends(body(ResultManifest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.check(caller, "cache_write", domain=domain, owner=None)
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
    deps.check(caller, "cache_write", domain=domain, owner=None)
    replaced = deps.store.cache_replace_failed(
        domain, _digest(key, "cache_replace_failed key"), result
    )
    return ok(Changed(changed=replaced))


@router.get("/outputs/{object_id}", response_model=OptionalDigest)
def resolve_output(domain: str, object_id: str, deps: Deps, caller: Caller) -> Response:
    digest = _digest(object_id, "resolve_output object_id")
    if not _member(deps, caller, domain):
        return ok(OptionalDigest(digest=None))
    return ok(OptionalDigest(digest=deps.store.resolve_output(domain, digest)))


@router.post("/touch", status_code=204, openapi_extra=json_body(DigestBatch))
def touch(
    domain: str,
    batch: Annotated[DigestBatch, Depends(body(DigestBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.check(caller, "cache_write", domain=domain, owner=None)
    deps.store.touch(domain, batch.digests)
    return no_content()


@router.post("/blobs", status_code=204, openapi_extra=json_body(BlobBatch))
def register_blobs(
    domain: str,
    batch: Annotated[BlobBatch, Depends(body(BlobBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.check(caller, "cache_write", domain=domain, owner=None)
    deps.store.register_blobs(domain, batch.blobs)
    return no_content()
