"""Release, channel and export routes (P2-02 R5).

Writes are authorized as `release_write` / `release_export` on the owner of the release's build.
The store checks a new release against its build; a channel move runs the service's own gate
policy (`ServiceDeps.gates`), whatever the client's config says. An export only records the
release in the target domain and its audit row: the client copies the bytes, since the service
has no CAS access (`ebs.release.export`).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.core.errors import MetadataError, ReleaseError
from ebs.meta.api import (
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
from ebs.meta.service.auth import Operation, Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import ok
from ebs.release.channels import move_channel

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(tags=["releases"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _release(deps: ServiceDeps, release: int) -> ReleaseView:
    found = deps.store.get_release_by_id(ReleaseId(release))
    if found is None:
        raise MetadataError(f"no release with id {release}")
    return found


def _write(deps: ServiceDeps, caller: Principal, build: int, op: Operation, domain: str) -> None:
    deps.authorize(caller, op, domain=domain, owner=deps.owner(build))


def _read(deps: ServiceDeps, caller: Principal, domain: str) -> None:
    deps.authorize(caller, "read", domain=check_domain(domain), owner=None)


@router.post("/releases", response_model=ReleaseView, openapi_extra=json_body(ReleaseCreate))
def create_release(
    spec: Annotated[ReleaseCreate, Depends(body(ReleaseCreate))], deps: Deps, caller: Caller
) -> Response:
    _write(deps, caller, spec.build, "release_write", spec.domain)
    return ok(deps.store.create_release(spec, by=caller.name))


@router.get("/releases", response_model=ReleaseList)
def list_releases(
    domain: str, deps: Deps, caller: Caller, name: str | None = None, version: str | None = None
) -> Response:
    _read(deps, caller, domain)
    if version is not None:
        if name is None:
            raise MetadataError("list_releases: a version needs a name")
        found = deps.store.get_release(domain, name, version)
        return ok(ReleaseList(releases=[] if found is None else [found]))
    return ok(ReleaseList(releases=deps.store.list_releases(domain, name=name)))


@router.get("/releases/{release}", response_model=ReleaseList)
def get_release(release: int, deps: Deps, caller: Caller) -> Response:
    found = deps.store.get_release_by_id(ReleaseId(release))
    if found is not None:
        _read(deps, caller, found.domain)
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
    found = _release(deps, release)
    _write(deps, caller, found.build, "release_write", found.domain)
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
    found = _release(deps, release)
    _write(deps, caller, found.build, "release_export", req.to_domain)
    return ok(deps.store.record_export(found.id, req.to_domain, by=caller.name))


@router.get("/releases/{release}/exports", response_model=ExportList)
def list_exports(release: int, deps: Deps, caller: Caller) -> Response:
    _read(deps, caller, _release(deps, release).domain)
    return ok(ExportList(exports=deps.store.list_exports(ReleaseId(release))))


@router.put("/channels", response_model=ChannelMove, openapi_extra=json_body(ChannelSet))
def set_channel(
    req: Annotated[ChannelSet, Depends(body(ChannelSet))], deps: Deps, caller: Caller
) -> Response:
    found = _release(deps, req.release)
    if (found.domain, found.name) != (req.domain, req.name):
        raise MetadataError(
            f"channel {req.name}:{req.channel} in domain {req.domain!r}: release {found.id} is "
            f"{found.name} {found.version} in domain {found.domain!r}; a channel points at "
            "releases of its own name and domain"
        )
    _write(deps, caller, found.build, "release_write", found.domain)
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
def channel_history(domain: str, name: str, channel: str, deps: Deps, caller: Caller) -> Response:
    _read(deps, caller, domain)
    return ok(ChannelHistory(moves=deps.store.channel_history(domain, name, channel)))


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
