"""The metadata service in-process, for contract and route tests (P1-06).

`make_service` serves a backing store through the FastAPI app; `Service.store()` is an
`HttpMetadataStore` talking to it over Starlette's in-process transport: no sockets, no network.
Authentication uses `FakeMungeVerifier`, which behaves like `unmunge` (each credential decodes
once; replays and expired credentials are refused), and `FakeMunge`, the client-side source that
mints a fresh credential bound to each request, as `munge --string <munge_payload>` does.
"""

from __future__ import annotations

import itertools
import threading
import warnings
from collections.abc import Callable, Generator, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.exceptions import StarletteDeprecationWarning

from ebs.meta.api import API_PREFIX
from ebs.meta.client import HttpMetadataStore, munge_payload
from ebs.meta.service.app import ServiceDeps, create_app
from ebs.meta.service.auth import (
    MUNGE_HEADER,
    AuthenticationError,
    Authenticator,
    AuthorizeHook,
    BearerAuthenticator,
    ChainAuthenticator,
    MungeAuthenticator,
    MungeIdentity,
    Policy,
)
from ebs.meta.service.deps import ServiceBackend
from ebs.meta.service.oidc import OidcLogin
from ebs.meta.service.tokens import InMemoryTokenStore
from ebs.release.channels import GatePolicy

with warnings.catch_warnings():
    # Starlette 1.x deprecates `httpx` for its test client in favour of `httpx2`; the transport
    # still works and httpx is the client the project depends on.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

BASE_URL = "http://meta.test"
AUDIENCE = "meta.test"  # MUNGE binding: the host[:port] of BASE_URL
USERS: Mapping[int, str] = {1000: "alice", 1001: "bob", 1002: "carol", 1003: "dave"}


@dataclass
class FakeMungeVerifier:
    """`unmunge` stand-in: `cred:<uid>:<serial>:<payload>` decodes once; `expired:` never."""

    seen: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def verify(self, credential: str) -> MungeIdentity:
        kind, _, rest = credential.partition(":")
        if kind == "expired":
            raise AuthenticationError("MUNGE credential refused: Expired credential")
        if kind != "cred":
            raise AuthenticationError("MUNGE credential refused: Invalid credential format")
        with self.lock:
            if credential in self.seen:
                raise AuthenticationError("MUNGE credential refused: Replayed credential")
            self.seen.add(credential)
        uid, _serial, payload = [*rest.split(":", 2), "", ""][:3]
        return MungeIdentity(uid=int(uid), gid=int(uid), payload=payload)


class FakeMunge(httpx.Auth):
    """Client credential source: a new `cred:<uid>:<n>:<payload>` per request, bound like
    `MungeCredential` (`munge_payload`). Also an `httpx.Auth` for raw test-client requests."""

    def __init__(self, uid: int = 1000, audience: str = AUDIENCE) -> None:
        self.uid = uid
        self.audience = audience
        self._serial = itertools.count()
        self._lock = threading.Lock()

    def headers(self, method: str, path: str) -> dict[str, str]:
        with self._lock:
            n = next(self._serial)
        payload = munge_payload(self.audience, method, path)
        return {MUNGE_HEADER: f"cred:{self.uid}:{n}:{payload}"}

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        path = request.url.path
        at = path.find(API_PREFIX)
        request.headers.update(
            self.headers(request.method, path[at + len(API_PREFIX) :] if at >= 0 else path)
        )
        yield request


def user_of_uid(uid: int) -> str:
    try:
        return USERS[uid]
    except KeyError:
        raise AuthenticationError(f"uid {uid} has no user name") from None


class ReadyProbe:
    """Readiness stand-in: `reason` None means ready."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason

    def check(self) -> str | None:
        return self.reason


@dataclass
class Service:
    deps: ServiceDeps
    client: TestClient
    tokens: InMemoryTokenStore
    munge: FakeMungeVerifier
    ready: ReadyProbe

    def store(self, credentials: object = None, **kwargs: object) -> HttpMetadataStore:
        """An HttpMetadataStore on this service (alice's MUNGE identity by default)."""
        return HttpMetadataStore(
            BASE_URL,
            credentials=FakeMunge() if credentials is None else credentials,  # type: ignore[arg-type]
            client=self.client,
            sleep=lambda _s: None,
            **kwargs,  # type: ignore[arg-type]
        )


def make_service(
    backing: ServiceBackend,
    *,
    tokens: InMemoryTokenStore | None = None,
    users: Callable[[int], str] = user_of_uid,
    toolchain_admins: Iterable[str] = (),
    gates: GatePolicy | None = None,
    authorize: AuthorizeHook | None = None,
    oidc: OidcLogin | None = None,
    **options: Any,
) -> Service:
    """`options` are further `ServiceDeps` fields (P2-05: events, limits, limiter, metrics)."""
    tokens = tokens if tokens is not None else InMemoryTokenStore()
    munge = FakeMungeVerifier()
    ready = ReadyProbe()
    schemes: list[Authenticator] = [
        MungeAuthenticator(munge, users, audience=AUDIENCE),
        BearerAuthenticator(tokens),
    ]
    if oidc is not None:
        schemes.append(oidc.sessions)
    deps = ServiceDeps(
        store=backing,
        authenticator=ChainAuthenticator(*schemes),
        readiness=ready,
        authorize=authorize or Policy(toolchain_admins),
        gates=gates or GatePolicy(),
        oidc=oidc,
        **options,
    )
    client = TestClient(create_app(deps), base_url=BASE_URL, raise_server_exceptions=False)
    return Service(deps, client, tokens, munge, ready)
