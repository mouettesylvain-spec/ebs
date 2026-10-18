"""The metadata service: FastAPI app over a MetadataStore (P1-06, interfaces.md § 6).

`create_app(deps)` builds the app; `app_from_config()` is the uvicorn factory for deployments
(`uvicorn --factory ebs.meta.service.app:app_from_config`). Routes live under `API_PREFIX`, with
the OpenAPI document at `/api/v1/openapi.json`; `/healthz` and `/readyz` are probes (R6),
`/metrics` the Prometheus metrics and `/auth/*` the dashboard login (P2-05). With
`[meta_service].dashboard_dir` the built dashboard (`web/dist`, P2-06) is served at `/ui/`.

Error mapping (every body is `ErrorBody`): AuthenticationError 401, PermissionDeniedError 403,
NotFoundError 404, a body over the limit 413, invalid request 422, a rate limit 429 with
`Retry-After`, a database outage 503 (the client retries idempotent calls), any other
MetadataError 409 with the store's message, anything else 500 without internals.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
)

import ebs
from ebs.config import config_paths, load_config
from ebs.core.errors import ConfigError, MetadataError
from ebs.core.log import configure_logging, get_logger
from ebs.domains import Domains
from ebs.meta.api import API_PREFIX, ErrorBody
from ebs.meta.pg import PgMetadataStore
from ebs.meta.service.auth import (
    AuthenticationError,
    Authenticator,
    BearerAuthenticator,
    ChainAuthenticator,
    MungeAuthenticator,
    NotFoundError,
    PermissionDeniedError,
    Policy,
    UnmungeVerifier,
)
from ebs.meta.service.authz import DomainPolicy, NssGroups
from ebs.meta.service.deps import BODY_MODELS, ServiceDeps, is_outage
from ebs.meta.service.events_stream import EventStreamConfig, PgEventListener
from ebs.meta.service.limits import (
    BodyLimit,
    LimitsConfig,
    RateLimitedError,
    retry_after,
)
from ebs.meta.service.logs import LogSource
from ebs.meta.service.metrics import Metrics, ObservabilityMiddleware, PoolStats
from ebs.meta.service.oidc import OidcConfig, OidcLogin
from ebs.meta.service.readiness import PgReadiness
from ebs.meta.service.routes import auth as auth_routes
from ebs.meta.service.routes import (
    builds,
    cache,
    dashboard,
    events,
    probes,
    provenance,
    releases,
    toolchains,
)
from ebs.meta.service.tokens import PgTokenStore
from ebs.release.channels import GatePolicy
from ebs.runner.config import RunnerSettings

__all__ = ["ServiceConfig", "ServiceDeps", "app_from_config", "create_app"]

_log = get_logger(__name__)

_ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorBody} for code in (401, 403, 404, 409, 413, 422, 429, 503)
}


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


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return _error(404, str(exc))


async def _rate_limited(request: Request, exc: Exception) -> JSONResponse:
    wait = exc.retry_after_s if isinstance(exc, RateLimitedError) else 1.0
    _log.info("rate limited", path=request.url.path, error=str(exc))
    return _error(429, str(exc), {"Retry-After": retry_after(wait)})


async def _fail_closed(request: Request, call_next: Any) -> Any:
    """Refuse to send a success that no `ServiceDeps.check` authorized (P2-01 R1): a route that
    forgot its authorization fails loudly instead of leaking data. It guards what is sent, not
    what the route already wrote (and clients retry 5xx), so the route tests and the matrix
    (tests/helpers/authz_matrix.py) remain what proves each route authorizes first."""
    response = await call_next(request)
    principal = getattr(request.state, "principal", None)
    if principal is not None and response.status_code < 400 and not principal.authorized:
        _log.error("route answered without authorization", path=request.url.path)
        return _error(500, "internal error: the route did not authorize the request")
    return response


async def _store_error(request: Request, exc: Exception) -> JSONResponse:
    if is_outage(exc):
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


def create_app(
    deps: ServiceDeps,
    *,
    listener: PgEventListener | None = None,
    dashboard_dir: Path | None = None,
) -> FastAPI:
    """The app; `listener` (PostgreSQL NOTIFY wake-ups) runs while the app is served, and
    `dashboard_dir` (the dashboard's static build) is served at `/ui/`."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if listener is not None:
            listener.start()
        try:
            yield
        finally:
            if listener is not None:
                listener.stop()

    app = FastAPI(
        title="EBS metadata service",
        version=ebs.__version__,
        description="Shared build state of the EDA Build System (docs/design/interfaces.md § 6).",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.deps = deps
    app.include_router(probes.probes)
    if deps.oidc is not None:
        app.include_router(auth_routes.router)
    routers = (probes.api, cache.router, builds.router, events.router, toolchains.router)
    for router in (*routers, releases.router, provenance.router, dashboard.router):
        app.include_router(router, prefix=API_PREFIX, responses=_ERRORS)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus metrics (no credentials, like the probes)."""
        return Response(
            deps.metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    if dashboard_dir is not None:
        app.mount("/ui", StaticFiles(directory=dashboard_dir, html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        def home() -> Response:
            return RedirectResponse("/ui/", status_code=307)

    app.add_exception_handler(AuthenticationError, _auth_failed)
    app.add_exception_handler(PermissionDeniedError, _forbidden)
    app.add_exception_handler(NotFoundError, _not_found)
    app.add_exception_handler(MetadataError, _store_error)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(RateLimitedError, _rate_limited)
    app.add_exception_handler(Exception, _internal)
    app.middleware("http")(_fail_closed)
    # The last added runs first: request ids and timing wrap everything, the body limit wraps
    # the routes.
    app.add_middleware(BodyLimit, max_bytes=deps.limits.max_body_bytes)
    app.add_middleware(ObservabilityMiddleware, metrics=deps.metrics)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    return app


# --- deployment -----------------------------------------------------------------------------------


class ServiceConfig(BaseModel):
    """The `[meta_service]` section of ebs.toml on the service host."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    munge: StrictBool = True
    unmunge: StrictStr = "unmunge"
    audience: StrictStr | None = None  # required with munge: the host[:port] clients use
    bearer: StrictBool = True
    toolchain_admins: list[StrictStr] = Field(default_factory=list)
    # P2-01: the domains this service serves (each in [[domains]]), bearer token principals'
    # domains, and how long NSS group lookups are cached.
    domains: list[StrictStr] = Field(default_factory=list)
    token_domains: dict[StrictStr, list[StrictStr]] = Field(default_factory=dict)
    group_ttl_s: Annotated[StrictInt, Field(ge=0)] = 60
    # P2-05: request limits, the event stream and its retention, and the dashboard login.
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    events: EventStreamConfig = Field(default_factory=EventStreamConfig)
    oidc: OidcConfig | None = None
    dashboard_dir: StrictStr | None = None  # P2-06: the built dashboard, served at /ui/
    log_level: StrictStr = "INFO"  # JSON lines on stderr, with request ids (R5)

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
    configure_logging(service.log_level, json=True)
    schemes: list[Authenticator] = []
    if service.munge:
        if not service.audience:
            raise ConfigError(
                "[meta_service].audience must be set with munge = true: the host[:port] of the "
                "service URL clients use (MUNGE credentials are bound to it)"
            )
        verifier = UnmungeVerifier(service.unmunge)
        schemes.append(MungeAuthenticator(verifier, audience=service.audience))
    if service.bearer:
        schemes.append(BearerAuthenticator(PgTokenStore(store.engine)))
    if not schemes:
        raise ConfigError("[meta_service] enables no authentication: set munge or bearer")
    oidc = None if service.oidc is None else OidcLogin.from_config(service.oidc)
    if oidc is not None:
        schemes.append(oidc.sessions)  # the dashboard's session cookie, last
    deps = ServiceDeps(
        store=store,
        authenticator=ChainAuthenticator(*schemes),
        readiness=PgReadiness(store.engine),
        authorize=DomainPolicy(
            Policy(service.toolchain_admins),
            Domains.from_config(config),
            service.domains,
            NssGroups(ttl_s=service.group_ttl_s),
            service.token_domains,
        ),
        gates=GatePolicy.from_section(config.get("release", {})),
        events=service.events,
        limits=service.limits,
        metrics=Metrics(pool=lambda: _pool_stats(store)),
        oidc=oidc,
        logs=_log_source(config, env),
    )
    dashboard = None if service.dashboard_dir is None else Path(service.dashboard_dir)
    if dashboard is not None and not (dashboard / "index.html").is_file():
        raise ConfigError(
            f"[meta_service].dashboard_dir {dashboard} has no index.html: build the dashboard "
            "(cd web && npm ci && npm run build) and point it at web/dist"
        )
    return create_app(
        deps, listener=PgEventListener(store.engine.url, deps.wakeup), dashboard_dir=dashboard
    )


def _log_source(
    config: Mapping[str, Mapping[str, object]], env: Mapping[str, str]
) -> LogSource | None:
    """Logs from the CAS and debug roots of `[cas]`, `[debug]` and `[[domains]]`; None without
    `[cas].root` (the dashboard then shows no logs)."""
    if "root" not in config.get("cas", {}):
        return None
    settings = RunnerSettings.from_config(config, env)
    return LogSource(settings.cas_root_for, settings.debug_root_for)


def _pool_stats(store: PgMetadataStore) -> PoolStats:
    pool = store.engine.pool
    size = getattr(pool, "size", lambda: 0)()
    out = getattr(pool, "checkedout", lambda: 0)()
    overflow = getattr(pool, "overflow", lambda: 0)()
    return PoolStats(size=int(size), checked_out=int(out), overflow=max(0, int(overflow)))
