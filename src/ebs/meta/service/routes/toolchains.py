"""Toolchain registry routes (P1-07 store methods over HTTP)."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from ebs.core.digest import Digest
from ebs.core.errors import DigestError, MetadataError
from ebs.meta.api import (
    RegisterOutcome,
    ToolchainList,
    ToolchainManifest,
    ToolchainRegistration,
)
from ebs.meta.service.auth import Principal
from ebs.meta.service.deps import ServiceDeps, body, current_principal, get_deps, json_body
from ebs.meta.service.routes import no_content, ok

# Authenticate before any body is read or validated (401, never 422, without credentials).
router = APIRouter(
    prefix="/toolchains", tags=["toolchains"], dependencies=[Depends(current_principal)]
)

Deps = Annotated[ServiceDeps, Depends(get_deps)]
Caller = Annotated[Principal, Depends(current_principal)]


def _id(value: str) -> Digest:
    try:
        return Digest.parse(value)
    except DigestError as exc:
        raise MetadataError(f"toolchain id: {exc}") from None


@router.post("", response_model=RegisterOutcome, openapi_extra=json_body(ToolchainRegistration))
def register_toolchain(
    reg: Annotated[ToolchainRegistration, Depends(body(ToolchainRegistration))],
    deps: Deps,
    caller: Caller,
) -> Response:
    deps.authorize(caller, "toolchain_write", domain=None, owner=None)
    try:
        manifest = base64.b64decode(reg.manifest_b64, validate=True)
    except binascii.Error as exc:
        raise MetadataError(f"register_toolchain: manifest_b64 is not base64: {exc}") from None
    return ok(deps.store.register_toolchain(reg.spec, manifest))


@router.get("", response_model=ToolchainList)
def list_toolchains(
    deps: Deps, caller: Caller, module: str | None = None, current_only: bool = False
) -> Response:
    deps.authorize(caller, "read", domain=None, owner=None)
    found = deps.store.list_toolchains(module=module, current_only=current_only)
    return ok(ToolchainList(toolchains=found))


@router.get("/current", response_model=ToolchainList)
def current_toolchain(module: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "read", domain=None, owner=None)
    found = deps.store.current_toolchain(module)
    return ok(ToolchainList(toolchains=[] if found is None else [found]))


@router.get("/{toolchain_id}", response_model=ToolchainList)
def get_toolchain(toolchain_id: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "read", domain=None, owner=None)
    found = deps.store.get_toolchain(_id(toolchain_id))
    return ok(ToolchainList(toolchains=[] if found is None else [found]))


@router.get("/{toolchain_id}/manifest", response_model=ToolchainManifest)
def toolchain_manifest(toolchain_id: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "read", domain=None, owner=None)
    found = deps.store.toolchain_manifest(_id(toolchain_id))
    encoded = None if found is None else base64.b64encode(found).decode("ascii")
    return ok(ToolchainManifest(manifest_b64=encoded))


@router.post("/{toolchain_id}/touch", status_code=204)
def touch_toolchain(toolchain_id: str, deps: Deps, caller: Caller) -> Response:
    deps.authorize(caller, "cache_write", domain=None, owner=None)
    deps.store.touch_toolchain(_id(toolchain_id))
    return no_content()
