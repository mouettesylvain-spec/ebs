"""Release, channel, export and archive routes (P2-02 R5, P3-04 R2).

Writes are authorized as `release_write` / `release_export` on the owner of the release's build.
The store checks a new release against its build; a channel move runs the service's own gate
policy (`ServiceDeps.gates`), whatever the client's config says. An export only records the
release in the target domain and its audit row: the client copies the bytes, since the service
has no CAS access (`ebs.release.export`).
"""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.core.errors import MetadataError, ReleaseError
from ebs.meta.api import (
    ArchiveList,
    ArchiveRecord,
    ArchiveRequest,
    BuildId,
    BuildList,
    ChannelHistory,
    ChannelList,
    ChannelMove,
    ChannelSet,
    ExportList,
    ExportRequest,
    GateQuery,
    ReleaseCreate,
    ReleaseId,
    ReleaseList,
    ReleaseView,
    YankRequest,
    check_domain,
)
from ebs.meta.service.auth import BuildOwner, NotFoundError, Operation, Principal
from ebs.meta.service.authz import DomainDenied
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.pagination import Cursor, Limit, paginate, sort_ts
from ebs.meta.service.routes import ok
from ebs.release.channels import move_channel

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(tags=["releases"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _hidden(deps: ServiceDeps, caller: Principal, release: ReleaseView, op: Operation) -> None:
    """Authorize `op` on a release named by id; outside the caller's domains it does not exist
    (404, R2)."""
    try:
        deps.check(caller, op, domain=release.domain, owner=_owner(deps, release))
    except DomainDenied:
        raise NotFoundError(f"no release with id {release.id}") from None


def _owner(deps: ServiceDeps, release: ReleaseView) -> BuildOwner:
    """The owner rules of the release's build, in the release's own domain: an exported release
    keeps its source build, whose domain its readers need not belong to."""
    return dataclasses.replace(deps.owner(release.build), domain=release.domain)


def _release(deps: ServiceDeps, caller: Principal, release: int, op: Operation) -> ReleaseView:
    found = deps.store.get_release_by_id(ReleaseId(release))
    if found is None:
        raise NotFoundError(f"no release with id {release}")
    _hidden(deps, caller, found, op)
    return found


def _read(deps: ServiceDeps, caller: Principal, domain: str) -> None:
    deps.check(caller, "read", domain=check_domain(domain), owner=None)


@router.post("/releases", response_model=ReleaseView, openapi_extra=json_body(ReleaseCreate))
def create_release(
    spec: Annotated[ReleaseCreate, Depends(body(ReleaseCreate))], deps: Deps, caller: Caller
) -> Response:
    # The release's domain first (403), so a non-member learns nothing about build ids.
    deps.check(caller, "read", domain=spec.domain, owner=None)
    try:
        deps.check(caller, "release_write", domain=spec.domain, owner=deps.owner(spec.build))
    except DomainDenied:  # the build is in a domain the caller is not in: no such build
        raise NotFoundError(f"no build with id {spec.build}") from None
    return ok(deps.store.create_release(spec, by=caller.name))


@router.get("/releases", response_model=ReleaseList)
def list_releases(
    domain: str,
    deps: Deps,
    caller: Caller,
    name: str | None = None,
    version: str | None = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    _read(deps, caller, domain)
    if version is not None:
        if name is None:
            raise MetadataError("list_releases: a version needs a name")
        found = deps.store.get_release(domain, name, version)
        listed = [] if found is None else [found]
    else:
        listed = deps.store.list_releases(domain, name=name)
    page, after = paginate(
        listed,
        lambda r: (r.name, sort_ts(r.created_at), int(r.id)),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "releases", "domain": domain, "name": name, "version": version},
    )
    return ok(ReleaseList(releases=page, next=after))


@router.get("/releases/{release}", response_model=ReleaseList)
def get_release(release: int, deps: Deps, caller: Caller) -> Response:
    found = deps.store.get_release_by_id(ReleaseId(release))
    if found is not None:
        try:
            _hidden(deps, caller, found, "read")
        except NotFoundError:
            found = None  # the same answer as for an id that does not exist
    if found is None:
        deps.check(caller, "read", domain=None, owner=None)
    return ok(ReleaseList(releases=[] if found is None else [found]))


@router.post(
    "/releases/{release}/yank", response_model=ReleaseView, openapi_extra=json_body(YankRequest)
)
def yank_release(
    release: int,
    req: Annotated[YankRequest, Depends(body(YankRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    found = _release(deps, caller, release, "release_write")
    return ok(deps.store.yank_release(found.id, by=caller.name, reason=req.reason))


@router.post(
    "/releases/{release}/exports",
    response_model=ReleaseView,
    openapi_extra=json_body(ExportRequest),
)
def record_export(
    release: int,
    req: Annotated[ExportRequest, Depends(body(ExportRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    found = _release(deps, caller, release, "release_write")  # the source domain
    deps.check(caller, "release_export", domain=req.to_domain, owner=_owner(deps, found))
    return ok(deps.store.record_export(found.id, req.to_domain, by=caller.name))


@router.get("/releases/{release}/exports", response_model=ExportList)
def list_exports(
    release: int, deps: Deps, caller: Caller, limit: Limit = None, cursor: Cursor = None
) -> Response:
    _release(deps, caller, release, "read")
    page, after = paginate(
        deps.store.list_exports(ReleaseId(release)),
        lambda x: (x.id,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "exports", "release": release},
    )
    return ok(ExportList(exports=page, next=after))


@router.post(
    "/releases/{release}/archives",
    response_model=ArchiveRecord,
    openapi_extra=json_body(ArchiveRequest),
)
def record_archive(
    release: int,
    req: Annotated[ArchiveRequest, Depends(body(ArchiveRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    found = _release(deps, caller, release, "release_write")
    return ok(deps.store.record_archive(found.id, req.bundle, req.location, by=caller.name))


@router.get("/releases/{release}/archives", response_model=ArchiveList)
def list_archives(
    release: int, deps: Deps, caller: Caller, limit: Limit = None, cursor: Cursor = None
) -> Response:
    _release(deps, caller, release, "read")
    page, after = paginate(
        deps.store.list_archives(ReleaseId(release)),
        lambda x: (x.id,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "archives", "release": release},
    )
    return ok(ArchiveList(archives=page, next=after))


@router.put("/channels", response_model=ChannelMove, openapi_extra=json_body(ChannelSet))
def set_channel(
    req: Annotated[ChannelSet, Depends(body(ChannelSet))], deps: Deps, caller: Caller
) -> Response:
    _read(deps, caller, req.domain)
    found = _release(deps, caller, req.release, "release_write")
    if (found.domain, found.name) != (req.domain, req.name):
        raise MetadataError(
            f"channel {req.name}:{req.channel} in domain {req.domain!r}: release {found.id} is "
            f"{found.name} {found.version} in domain {found.domain!r}; a channel points at "
            "releases of its own name and domain"
        )
    gate_build = None if req.gate_build is None else BuildId(req.gate_build)
    try:
        move = move_channel(
            deps.store, deps.gates, found, req.channel, by=caller.name, gate_build=gate_build
        )
    except ReleaseError as exc:  # a failed gate: refused like any other store rule (409)
        raise MetadataError(str(exc)) from exc
    return ok(move)


@router.get("/channels", response_model=ChannelList)
def get_channel(domain: str, name: str, channel: str, deps: Deps, caller: Caller) -> Response:
    _read(deps, caller, domain)
    found = deps.store.get_channel(domain, name, channel)
    return ok(ChannelList(channels=[] if found is None else [found]))


@router.get("/channels/history", response_model=ChannelHistory)
def channel_history(
    domain: str,
    name: str,
    channel: str,
    deps: Deps,
    caller: Caller,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    _read(deps, caller, domain)
    page, after = paginate(
        deps.store.channel_history(domain, name, channel),
        lambda m: (m.id,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "channel_history", "domain": domain, "name": name, "channel": channel},
    )
    return ok(ChannelHistory(moves=page, next=after))


@router.post(
    "/domains/{domain}/gate-builds", response_model=BuildList, openapi_extra=json_body(GateQuery)
)
def gate_builds(
    domain: str,
    query: Annotated[GateQuery, Depends(body(GateQuery))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _read(deps, caller, domain)
    found = deps.store.gate_builds(domain, query.flow, query.ids)
    return ok(BuildList(builds=[int(b) for b in found]))
