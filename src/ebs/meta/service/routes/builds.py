"""Build, action, result and event routes; every write checks the build's owner (R4)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.exceptions import RequestValidationError

from ebs.core.errors import MetadataError
from ebs.meta.api import (
    LOG_CHUNK_MAX,
    ActionBatch,
    ActionList,
    ActionState,
    BuildCreate,
    BuildFinish,
    BuildId,
    BuildView,
    Changed,
    CreatedBuild,
    CreatedLease,
    DriverClaim,
    DriverLease,
    DriverRelease,
    Event,
    LeaseId,
    LeaseRequest,
    OptionalResult,
    PinRequest,
    ReleaseImportBatch,
    ReleaseImportList,
    ResultManifest,
    ResultSummaryList,
    StateChange,
)
from ebs.meta.service.auth import BuildOwner, NotFoundError, Operation, Principal
from ebs.meta.service.authz import DomainDenied
from ebs.meta.service.deps import (
    ServiceDeps,
    body,
    current_principal,
    get_deps,
    is_outage,
    json_body,
)
from ebs.meta.service.pagination import Cursor, Limit, paginate
from ebs.meta.service.routes import no_content, ok

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(prefix="/builds", tags=["builds"], dependencies=[Depends(current_principal)])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]

# `set_action_state` takes these positionally; as keyword fields they would collide.
_RESERVED_FIELDS = frozenset({"self", "build", "action_id", "state"})


def _check(deps: ServiceDeps, caller: Principal, build: int, op: Operation) -> None:
    """Authorize `op` on `build`; a build of a domain the caller is not in looks like no
    build at all (404), so ids do not reveal what exists (R2)."""
    owner = deps.owner(build)
    try:
        deps.check(caller, op, domain=owner.domain, owner=owner)
    except DomainDenied:
        raise NotFoundError(f"no build with id {build}") from None


def _write(deps: ServiceDeps, caller: Principal, build: int) -> None:
    _check(deps, caller, build, "build_write")


def _read(deps: ServiceDeps, caller: Principal, build: int) -> None:
    _check(deps, caller, build, "read")


@router.post("", response_model=CreatedBuild, openapi_extra=json_body(BuildCreate))
def create_build(
    spec: Annotated[BuildCreate, Depends(body(BuildCreate))], deps: Deps, caller: Caller
) -> Response:
    owner = BuildOwner(user_name=spec.user_name, project=spec.project, domain=spec.domain)
    deps.check(caller, "build_write", domain=spec.domain, owner=owner)
    return ok(CreatedBuild(id=deps.store.create_build(spec)))


@router.post(
    "/{build}/release-imports", status_code=204, openapi_extra=json_body(ReleaseImportBatch)
)
def record_release_imports(
    build: int,
    batch: Annotated[ReleaseImportBatch, Depends(body(ReleaseImportBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)  # the store keeps every release in the build's domain
    deps.store.record_release_imports(BuildId(build), batch.imports)
    return no_content()


@router.get("/{build}/release-imports", response_model=ReleaseImportList)
def list_release_imports(
    build: int, deps: Deps, caller: Caller, limit: Limit = None, cursor: Cursor = None
) -> Response:
    _read(deps, caller, build)
    page, after = paginate(
        deps.store.list_release_imports(BuildId(build)),
        lambda r: (r.alias,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "release_imports", "build": build},
    )
    return ok(ReleaseImportList(imports=page, next=after))


@router.get("/{build}", response_model=BuildView)
def get_build(build: int, deps: Deps, caller: Caller) -> Response:
    _read(deps, caller, build)
    return ok(deps.store.get_build(BuildId(build)))


@router.post("/{build}/actions", status_code=204, openapi_extra=json_body(ActionBatch))
def add_actions(
    build: int,
    batch: Annotated[ActionBatch, Depends(body(ActionBatch))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.add_actions(BuildId(build), batch.actions)
    return no_content()


@router.get("/{build}/actions", response_model=ActionList)
def list_actions(
    build: int,
    deps: Deps,
    caller: Caller,
    state: ActionState | None = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    _read(deps, caller, build)
    page, after = paginate(
        deps.store.list_actions(BuildId(build), state=state),
        lambda a: (a.action_id,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "actions", "build": build, "state": state},
    )
    return ok(ActionList(actions=page, next=after))


@router.post(
    "/{build}/actions/{action_id:path}/state",
    status_code=204,
    openapi_extra=json_body(StateChange),
)
def set_action_state(
    build: int,
    action_id: str,
    change: Annotated[StateChange, Depends(body(StateChange))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    clash = _RESERVED_FIELDS & set(change.fields)
    if clash:
        raise MetadataError(
            f"action {action_id!r}: invalid fields for state {change.state}: {sorted(clash)}"
        )
    deps.store.set_action_state(
        BuildId(build),
        action_id,
        change.state,  # type: ignore[arg-type]  # the store validates the state
        **change.fields,
    )
    return no_content()


@router.put(
    "/{build}/actions/{action_id:path}/result",
    status_code=204,
    openapi_extra=json_body(ResultManifest),
)
def record_result(
    build: int,
    action_id: str,
    result: Annotated[ResultManifest, Depends(body(ResultManifest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.record_result(BuildId(build), action_id, result)
    return no_content()


@router.get("/{build}/actions/{action_id:path}/result", response_model=OptionalResult)
def get_result(build: int, action_id: str, deps: Deps, caller: Caller) -> Response:
    _read(deps, caller, build)
    return ok(OptionalResult(result=deps.store.get_result(BuildId(build), action_id)))


@router.get("/{build}/results", response_model=ResultSummaryList)
def list_results(
    build: int,
    deps: Deps,
    caller: Caller,
    step: str | None = None,
    limit: Limit = None,
    cursor: Cursor = None,
) -> Response:
    """The recorded results of the build's actions, without inputs and outputs (P2-06 R2)."""
    _read(deps, caller, build)
    page, after = paginate(
        deps.store.result_summaries(BuildId(build), step=step),
        lambda r: (r.action_id,),
        cursor=cursor,
        limit=limit,
        max_page_size=deps.limits.max_page_size,
        query={"list": "results", "build": build, "step": step},
    )
    return ok(ResultSummaryList(results=page, next=after))


_LOG_DOC: dict[int | str, dict[str, object]] = {
    200: {
        "content": {"text/plain": {}},
        "description": "Log bytes from `offset`; `X-Ebs-Log-Source` (cas | live), "
        "`X-Ebs-Log-Size` (the whole log now) and `X-Ebs-Log-Complete` (false: may still grow).",
    }
}


@router.get("/{build}/actions/{action_id:path}/log", response_class=Response, responses=_LOG_DOC)
def get_log(
    build: int,
    action_id: str,
    deps: Deps,
    caller: Caller,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=LOG_CHUNK_MAX)] = LOG_CHUNK_MAX,
) -> Response:
    """The action's stored log, else its live log (P2-06 R3); 404 while it has neither."""
    _read(deps, caller, build)
    if deps.logs is None:
        raise NotFoundError(
            "this metadata service serves no logs: set [cas].root (and [debug].root for live "
            "logs) in its ebs.toml"
        )
    try:
        result = deps.store.get_result(BuildId(build), action_id)
    except MetadataError as exc:
        if is_outage(exc):
            raise
        raise NotFoundError(f"build {build} has no action {action_id!r}") from None
    chunk = deps.logs.read(
        deps.owner(build).domain,
        build,
        action_id,
        None if result is None else result.log,
        offset=offset,
        limit=limit,
    )
    if chunk is None:
        raise NotFoundError(
            f"no log of action {action_id!r} of build {build} yet (it has not started, or its "
            "log was not kept)"
        )
    return Response(
        content=chunk.data,
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Ebs-Log-Source": chunk.source,
            "X-Ebs-Log-Size": str(chunk.size),
            "X-Ebs-Log-Complete": "true" if chunk.complete else "false",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/{build}/finish", status_code=204, openapi_extra=json_body(BuildFinish))
def finish_build(
    build: int,
    finish: Annotated[BuildFinish, Depends(body(BuildFinish))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.finish_build(
        BuildId(build),
        finish.status,  # type: ignore[arg-type]
        final_plan=finish.final_plan,
    )
    deps.notify(build, deps.owner(build).domain)  # its streams end (P2-05 R2)
    return no_content()


@router.post("/{build}/events", status_code=204, openapi_extra=json_body(Event))
def emit(
    build: int,
    event: Annotated[Event, Depends(body(Event))],
    deps: Deps,
    caller: Caller,
) -> Response:
    if event.build != build:
        raise RequestValidationError(
            [{"loc": ("body", "build"), "msg": f"event is for build {event.build}, not {build}"}]
        )
    _write(deps, caller, build)
    deps.store.emit(event)
    deps.notify(build, deps.owner(build).domain)  # wake its streams (P2-05 R2)
    return no_content()


# --- leases and pins (P1-09 R2) ------------------------------------------------------------------


@router.post("/{build}/leases", response_model=CreatedLease, openapi_extra=json_body(LeaseRequest))
def acquire_lease(
    build: int,
    req: Annotated[LeaseRequest, Depends(body(LeaseRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    if req.domain is None:
        raise MetadataError("acquire_lease: the request must name the build's domain")
    lease = deps.store.acquire_lease(req.domain, BuildId(build), req.digests, req.ttl_s)
    return ok(CreatedLease(id=lease))


@router.post(
    "/{build}/leases/{lease}/renew", status_code=204, openapi_extra=json_body(LeaseRequest)
)
def renew_lease(
    build: int,
    lease: int,
    req: Annotated[LeaseRequest, Depends(body(LeaseRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.renew_lease(BuildId(build), LeaseId(lease), req.ttl_s, add=req.digests)
    return no_content()


# --- driver lease (P2-08 R3) ---------------------------------------------------------------------


@router.post("/{build}/driver", response_model=DriverLease, openapi_extra=json_body(DriverClaim))
def claim_driver(
    build: int,
    req: Annotated[DriverClaim, Depends(body(DriverClaim))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    return ok(deps.store.claim_driver(BuildId(build), req.owner, req.ttl_s))


@router.post("/{build}/driver/release", status_code=204, openapi_extra=json_body(DriverRelease))
def release_driver(
    build: int,
    req: Annotated[DriverRelease, Depends(body(DriverRelease))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.release_driver(BuildId(build), req.owner)
    return no_content()


@router.put("/{build}/pin", status_code=204, openapi_extra=json_body(PinRequest))
def pin_build(
    build: int,
    req: Annotated[PinRequest, Depends(body(PinRequest))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.pin_build(BuildId(build), by=caller.name, reason=req.reason)
    return no_content()


@router.delete("/{build}/pin", response_model=Changed)
def unpin_build(build: int, deps: Deps, caller: Caller) -> Response:
    _write(deps, caller, build)
    return ok(Changed(changed=deps.store.unpin_build(BuildId(build))))
