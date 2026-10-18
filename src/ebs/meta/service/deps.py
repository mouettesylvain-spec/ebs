"""What the metadata service routes depend on: the store, auth hooks and request parsing."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import psycopg
import sqlalchemy as sa
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import MetadataError
from ebs.meta.api import API_PREFIX, BuildId, DashboardReader, EventReader
from ebs.meta.service.auth import (
    Authenticator,
    AuthorizeHook,
    BuildOwner,
    NotFoundError,
    Operation,
    Principal,
    RequestInfo,
    authorize,
)
from ebs.meta.service.events_stream import EventStreamConfig, Wakeup, build_key, domain_key
from ebs.meta.service.limits import LimitsConfig, RateLimiter
from ebs.meta.service.logs import LogSource
from ebs.meta.service.metrics import Metrics
from ebs.meta.service.readiness import Readiness
from ebs.release.channels import GatePolicy
from ebs.release.releases import ReleaseBackend

if TYPE_CHECKING:
    from ebs.meta.service.oidc import OidcLogin

__all__ = [
    "BODY_MODELS",
    "ServiceBackend",
    "ServiceDeps",
    "body",
    "current_principal",
    "get_deps",
    "is_outage",
    "json_body",
]

_M = TypeVar("_M", bound=BaseModel)

_OUTAGES: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
    sa.exc.OperationalError,
    sa.exc.InterfaceError,
    sa.exc.TimeoutError,
    psycopg.OperationalError,
    psycopg.InterfaceError,
)


def is_outage(exc: BaseException) -> bool:
    """True if a MetadataError was caused by the database being unreachable or failing."""
    seen: BaseException | None = exc.__cause__
    while seen is not None:
        if isinstance(seen, _OUTAGES):
            return True
        seen = seen.__cause__
    return False


BODY_MODELS: set[type[BaseModel]] = set()
"""Request body models, published in the OpenAPI components by `app.create_app`."""


class ServiceBackend(ReleaseBackend, EventReader, DashboardReader, Protocol):
    """What the service needs of its store: the PostgreSQL store (the in-memory one in tests)."""


@dataclass
class ServiceDeps:
    """Everything the routes use; `create_app` stores it on `app.state.deps`.

    `authorize` is the single policy hook called by every API route (R4); P2-01 replaces it to add
    domain membership. Build owners are immutable, so they are cached (`owner_cache_size`).
    `gates` is the channel gate policy (`[release.gates]` of the service's config, P2-02).
    P2-05: `events` and `wakeup` drive the event streams, `limits` and `limiter` the request
    limits, `metrics` the Prometheus metrics, and `oidc` the dashboard login (None: off).
    P2-06: `logs` reads action logs for the dashboard (None: the log route answers 404).
    """

    store: ServiceBackend
    authenticator: Authenticator
    readiness: Readiness
    authorize: AuthorizeHook = authorize
    gates: GatePolicy = field(default_factory=GatePolicy)
    events: EventStreamConfig = field(default_factory=EventStreamConfig)
    wakeup: Wakeup = field(default_factory=Wakeup)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    limiter: RateLimiter | None = None  # None: the limits' rate, on the system clock
    metrics: Metrics = field(default_factory=Metrics)
    oidc: OidcLogin | None = None
    logs: LogSource | None = None
    clock: Clock = field(default_factory=SystemClock)  # P2-06: the stats route's default window
    owner_cache_size: int = 10_000
    _owners: OrderedDict[int, BuildOwner] = field(default_factory=OrderedDict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def check(
        self,
        principal: Principal,
        op: Operation,
        *,
        domain: str | None,
        owner: BuildOwner | None,
    ) -> None:
        """Call the `authorize` hook and record that this request was authorized (R1)."""
        self.authorize(principal, op, domain=domain, owner=owner)
        principal.authorized.append(op)

    def __post_init__(self) -> None:
        if self.limiter is None:
            self.limiter = RateLimiter(self.limits)

    def notify(self, build: int, domain: str) -> None:
        """Wake the event streams of `build` and of its domain (R2)."""
        self.wakeup.notify(build_key(build), domain_key(domain))

    def owner(self, build: int) -> BuildOwner:
        """The owner of `build`; NotFoundError (HTTP 404) if there is no such build."""
        with self._lock:
            cached = self._owners.get(build)
            if cached is not None:
                self._owners.move_to_end(build)
                return cached
        try:
            view = self.store.get_build(BuildId(build))
        except MetadataError as exc:
            if is_outage(exc):
                raise
            raise NotFoundError(f"no build with id {build}") from None
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
    deps = get_deps(request)
    principal = deps.authenticator.authenticate(info)
    if principal is None:  # pragma: no cover - ChainAuthenticator raises instead
        raise AssertionError("authenticator returned no principal")
    if deps.limiter is not None:
        # 429 past the rate (P2-05 R4); a Unix user has one bucket, by MUNGE or by session.
        kind = "token" if principal.via == "token" else "user"
        deps.limiter.check(f"{kind}:{principal.name}")
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
