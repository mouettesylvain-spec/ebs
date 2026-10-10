"""Build, action, result and event routes; every write checks the build's owner (R4)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.exceptions import RequestValidationError

from ebs.core.errors import MetadataError
from ebs.meta.api import (
    ActionBatch,
    ActionList,
    ActionState,
    BuildCreate,
    BuildFinish,
    BuildId,
    BuildView,
    CreatedBuild,
    Event,
    OptionalResult,
    ResultManifest,
    StateChange,
)
from ebs.meta.service.auth import BuildOwner, Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import no_content, ok

router = APIRouter(prefix="/builds", tags=["builds"])

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]

# `set_action_state` takes these positionally; as keyword fields they would collide.
_RESERVED_FIELDS = frozenset({"self", "build", "action_id", "state"})


def _write(deps: ServiceDeps, caller: Principal, build: int) -> None:
    owner = deps.owner(build)
    deps.authorize(caller, "build_write", domain=owner.domain, owner=owner)


def _read(deps: ServiceDeps, caller: Principal, build: int) -> None:
    owner = deps.owner(build)
    deps.authorize(caller, "read", domain=owner.domain, owner=owner)


@router.post("", response_model=CreatedBuild, openapi_extra=json_body(BuildCreate))
def create_build(
    spec: Annotated[BuildCreate, Depends(body(BuildCreate))], deps: Deps, caller: Caller
) -> Response:
    owner = BuildOwner(user_name=spec.user_name, project=spec.project, domain=spec.domain)
    deps.authorize(caller, "build_write", domain=spec.domain, owner=owner)
    return ok(CreatedBuild(id=deps.store.create_build(spec)))


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
    build: int, deps: Deps, caller: Caller, state: ActionState | None = None
) -> Response:
    _read(deps, caller, build)
    return ok(ActionList(actions=deps.store.list_actions(BuildId(build), state=state)))


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


@router.post("/{build}/finish", status_code=204, openapi_extra=json_body(BuildFinish))
def finish_build(
    build: int,
    finish: Annotated[BuildFinish, Depends(body(BuildFinish))],
    deps: Deps,
    caller: Caller,
) -> Response:
    _write(deps, caller, build)
    deps.store.finish_build(BuildId(build), finish.status)  # type: ignore[arg-type]
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
    return no_content()
