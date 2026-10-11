"""The metadata service in-process, for contract and route tests (P1-06).

`service_store` serves a backing MetadataStore through the FastAPI app and returns an
`HttpMetadataStore` talking to it over Starlette's in-process transport: no sockets, no network.
Authentication uses `FakeMungeVerifier`, which behaves like `unmunge` (each credential decodes
once; replays and expired credentials are refused), and `FakeMunge`, the client-side source that
mints a fresh credential per request as `munge -n` does.
"""

from __future__ import annotations

import itertools
import threading
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from starlette.exceptions import StarletteDeprecationWarning

from ebs.meta.api import MetadataStore
from ebs.meta.client import HttpMetadataStore
from ebs.meta.service.app import ServiceDeps, create_app
from ebs.meta.service.auth import (
    MUNGE_HEADER,
    AuthenticationError,
    BearerAuthenticator,
    ChainAuthenticator,
    MungeAuthenticator,
    MungeIdentity,
)
from ebs.meta.service.tokens import InMemoryTokenStore

with warnings.catch_warnings():
    # Starlette 1.x deprecates `httpx` for its test client in favour of `httpx2`; the transport
    # still works and httpx is the client the project depends on.
    warnings.simplefilter("ignore", StarletteDeprecationWarning)
    from starlette.testclient import TestClient

BASE_URL = "http://meta.test"
USERS: Mapping[int, str] = {1000: "alice", 1001: "bob", 1002: "carol"}


@dataclass
class FakeMungeVerifier:
    """`unmunge` stand-in: `cred:<uid>:<serial>` decodes once; `expired:` credentials never."""

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
        uid = int(rest.split(":")[0])
        return MungeIdentity(uid=uid, gid=uid)


class FakeMunge:
    """Client credential source: a new `cred:<uid>:<n>` per request."""

    def __init__(self, uid: int = 1000) -> None:
        self.uid = uid
        self._serial = itertools.count()
        self._lock = threading.Lock()

    def headers(self) -> dict[str, str]:
        with self._lock:
            n = next(self._serial)
        return {MUNGE_HEADER: f"cred:{self.uid}:{n}"}


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
    backing: MetadataStore,
    *,
    tokens: InMemoryTokenStore | None = None,
    users: Callable[[int], str] = user_of_uid,
) -> Service:
    tokens = tokens if tokens is not None else InMemoryTokenStore()
    munge = FakeMungeVerifier()
    ready = ReadyProbe()
    deps = ServiceDeps(
        store=backing,
        authenticator=ChainAuthenticator(
            MungeAuthenticator(munge, users), BearerAuthenticator(tokens)
        ),
        readiness=ready,
    )
    client = TestClient(create_app(deps), base_url=BASE_URL, raise_server_exceptions=False)
    return Service(deps, client, tokens, munge, ready)
