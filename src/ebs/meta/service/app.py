"""The metadata service: FastAPI app over a MetadataStore (P1-06, interfaces.md § 6).

`create_app(deps)` builds the app; `app_from_config()` is the uvicorn factory for deployments
(`uvicorn --factory ebs.meta.service.app:app_from_config`). Routes live under `API_PREFIX`, with
the OpenAPI document at `/api/v1/openapi.json`; `/healthz` and `/readyz` are probes (R6).

Error mapping (every body is `ErrorBody`): AuthenticationError 401, PermissionDeniedError 403,
invalid request 422, a database outage 503 (the client retries idempotent calls), any other
MetadataError 409 with the store's message, anything else 500 without internals.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import psycopg
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr, ValidationError

import ebs
from ebs.config import config_paths, load_config
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import get_logger
from ebs.meta.api import API_PREFIX, ErrorBody
from ebs.meta.pg import PgMetadataStore
from ebs.meta.service.auth import (
    AuthenticationError,
    Authenticator,
    BearerAuthenticator,
    ChainAuthenticator,
    MungeAuthenticator,
    PermissionDeniedError,
    UnmungeVerifier,
)
from ebs.meta.service.deps import BODY_MODELS, ServiceDeps
from ebs.meta.service.readiness import PgReadiness
from ebs.meta.service.routes import builds, cache, probes
from ebs.meta.service.tokens import PgTokenStore

__all__ = ["ServiceConfig", "ServiceDeps", "app_from_config", "create_app"]

_log = get_logger(__name__)

_OUTAGES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    sa.exc.OperationalError,
    sa.exc.InterfaceError,
    sa.exc.TimeoutError,
    psycopg.OperationalError,
    psycopg.InterfaceError,
)
_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorBody} for code in (401, 403, 409, 422, 503)
}


def _is_outage(exc: BaseException) -> bool:
    """True if a MetadataError was caused by the database being unreachable or failing."""
    seen: BaseException | None = exc.__cause__
    while seen is not None:
        if isinstance(seen, _OUTAGES):
            return True
        seen = seen.__cause__
    return False


def _error(status: int, detail: str, headers: Mapping[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        ErrorBody(detail=detail).model_dump(), status_code=status, headers=dict(headers or {})
    )


async def _auth_failed(request: Request, exc: Exception) -> JSONResponse:
    _log.info("authentication failed", path=request.url.path, error=str(exc))
    return _error(401, str(exc), {"WWW-Authenticate": 'Bearer realm="ebs-meta"'})


async def _forbidden(request: Request, exc: Exception) -> JSONResponse:
    principal = getattr(request.state, "principal", None)
    _log.warning("forbidden", path=request.url.path, principal=str(principal), error=str(exc))
    return _error(403, str(exc))


async def _store_error(request: Request, exc: Exception) -> JSONResponse:
    if _is_outage(exc):
        # The full text names the database host; it goes to the log, not to every caller.
        _log.error("metadata database unavailable", path=request.url.path, error=str(exc))
        return _error(503, "metadata database unavailable; retry later")
    return _error(409, str(exc))


async def _invalid(request: Request, exc: Exception) -> JSONResponse:
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    parts = []
    for e in errors:
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "body") or "(body)"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return _error(422, "invalid request: " + ("; ".join(parts) or str(exc)))


async def _internal(request: Request, exc: Exception) -> JSONResponse:
    _log.exception("internal error in the metadata service", path=request.url.path)
    return _error(500, "internal error in the metadata service; its log has the details")


def _openapi(app: FastAPI) -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title, version=app.version, description=app.description, routes=app.routes
    )
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in sorted(BODY_MODELS, key=lambda m: m.__name__):
        js = model.model_json_schema(ref_template="#/components/schemas/{model}")
        for name, sub in js.pop("$defs", {}).items():
            components.setdefault(name, sub)
        components[model.__name__] = js
    app.openapi_schema = schema
    return schema


def create_app(deps: ServiceDeps) -> FastAPI:
    app = FastAPI(
        title="EBS metadata service",
        version=ebs.__version__,
        description="Shared build state of the EDA Build System (docs/design/interfaces.md § 6).",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    app.state.deps = deps
    app.include_router(probes.probes)
    for router in (probes.api, cache.router, builds.router):
        app.include_router(router, prefix=API_PREFIX, responses=_ERRORS)
    app.add_exception_handler(AuthenticationError, _auth_failed)
    app.add_exception_handler(PermissionDeniedError, _forbidden)
    app.add_exception_handler(MetadataError, _store_error)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(Exception, _internal)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    return app


# --- deployment -----------------------------------------------------------------------------------


class ServiceConfig(BaseModel):
    """The `[meta_service]` section of ebs.toml on the service host."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    munge: StrictBool = True
    unmunge: StrictStr = "unmunge"
    bearer: StrictBool = True

    @classmethod
    def from_mapping(cls, section: Mapping[str, object]) -> ServiceConfig:
        try:
            return cls.model_validate(dict(section))
        except ValidationError as exc:
            raise ConfigError(f"invalid [meta_service] configuration: {exc}") from None


def app_from_config(environ: Mapping[str, str] | None = None) -> FastAPI:
    """The service as configured by ebs.toml: `[metadata].url` is the PostgreSQL database."""
    env = dict(os.environ if environ is None else environ)
    home = Path(env.get("HOME") or Path.home())
    config = load_config(config_paths(env, cwd=Path.cwd(), home=home))
    store = PgMetadataStore.from_config(config.get("metadata", {}))
    service = ServiceConfig.from_mapping(config.get("meta_service", {}))
    schemes: list[Authenticator] = []
    if service.munge:
        schemes.append(MungeAuthenticator(UnmungeVerifier(service.unmunge)))
    if service.bearer:
        schemes.append(BearerAuthenticator(PgTokenStore(store.engine)))
    if not schemes:
        raise ConfigError("[meta_service] enables no authentication: set munge or bearer")
    deps = ServiceDeps(
        store=store,
        authenticator=ChainAuthenticator(*schemes),
        readiness=PgReadiness(store.engine),
    )
    return create_app(deps)
