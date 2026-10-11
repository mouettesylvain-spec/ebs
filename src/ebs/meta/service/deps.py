"""What the metadata service routes depend on: the store, auth hooks and request parsing."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from ebs.meta.api import API_PREFIX, BuildId, MetadataStore
from ebs.meta.service.auth import (
    Authenticator,
    AuthorizeHook,
    BuildOwner,
    Principal,
    RequestInfo,
    authorize,
)
from ebs.meta.service.readiness import Readiness

__all__ = ["BODY_MODELS", "ServiceDeps", "body", "current_principal", "get_deps", "json_body"]

_M = TypeVar("_M", bound=BaseModel)

BODY_MODELS: set[type[BaseModel]] = set()
"""Request body models, published in the OpenAPI components by `app.create_app`."""


@dataclass
class ServiceDeps:
    """Everything the routes use; `create_app` stores it on `app.state.deps`.

    `authorize` is the single policy hook called by every API route (R4); P2-01 replaces it to add
    domain membership. Build owners are immutable, so they are cached (`owner_cache_size`).
    """

    store: MetadataStore
    authenticator: Authenticator
    readiness: Readiness
    authorize: AuthorizeHook = authorize
    owner_cache_size: int = 10_000
    _owners: OrderedDict[int, BuildOwner] = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def owner(self, build: int) -> BuildOwner:
        """The owner of `build`; MetadataError (HTTP 409) if there is no such build."""
        with self._lock:
            cached = self._owners.get(build)
            if cached is not None:
                self._owners.move_to_end(build)
                return cached
        view = self.store.get_build(BuildId(build))
        found = BuildOwner(
            user_name=view.user_name, project=view.project, domain=view.domain, build=build
        )
        with self._lock:
            self._owners[build] = found
            while len(self._owners) > self.owner_cache_size:
                self._owners.popitem(last=False)
        return found


def get_deps(request: Request) -> ServiceDeps:
    deps: ServiceDeps = request.app.state.deps
    return deps


def current_principal(request: Request) -> Principal:
    """The authenticated caller (sync: `unmunge` runs in the threadpool); 401 otherwise."""
    path: str = request.scope["path"]  # percent-decoded, as the client binds it
    at = path.find(API_PREFIX)
    info = RequestInfo(
        method=request.method,
        path=path[at + len(API_PREFIX) :] if at >= 0 else path,
        headers=request.headers,
    )
    principal = get_deps(request).authenticator.authenticate(info)
    if principal is None:  # pragma: no cover - ChainAuthenticator raises instead
        raise AssertionError("authenticator returned no principal")
    request.state.principal = principal
    return principal


def body(model: type[_M]) -> Callable[[Request], Awaitable[_M]]:
    """A dependency that parses the JSON body as `model` in JSON mode.

    FastAPI validates bodies in Python mode, where the strict `ebs.meta.api` models refuse ISO
    datetimes and UUID strings; parsing the raw bytes keeps the models strict and exact.
    """
    BODY_MODELS.add(model)

    async def parse(request: Request) -> _M:
        raw = await request.body()
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False)) from None

    return parse


def json_body(model: type[BaseModel]) -> dict[str, Any]:
    """`openapi_extra` documenting a route's request body as `model`."""
    BODY_MODELS.add(model)
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": f"#/components/schemas/{model.__name__}"}}
            },
        }
    }
