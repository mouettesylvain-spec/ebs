"""Authentication and authorization of the metadata service (P1-06 R3, R4; decision D1).

Two pluggable schemes, tried in order by `ChainAuthenticator`:

- MUNGE: cluster hosts send `X-EBS-Munge: <munge -n output>`; the service decodes it with
  `unmunge`, which refuses replayed, expired and rewound credentials, and maps the uid to a user
  name through NSS. This proves the Unix identity of runners and CLIs without any secret on disk.
- Bearer tokens (`Authorization: Bearer <token>`) for CI and services; only their SHA-256 is
  stored (`ebs.meta.service.tokens`).

Requests without valid credentials get 401. `authorize()` is the single policy hook every route
calls (R4): builds may only be changed by their owner (`user_name`) or by a CI token of the
build's project; reads are open to every authenticated principal until P2-01 adds domain
membership checks there.
"""

from __future__ import annotations

import pwd
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from ebs.core.errors import MetadataError
from ebs.core.log import get_logger
from ebs.meta.client import MUNGE_HEADER
from ebs.meta.service.tokens import TokenKind, TokenStore, hash_token

__all__ = [
    "MUNGE_HEADER",
    "AuthenticationError",
    "Authenticator",
    "AuthorizeHook",
    "BearerAuthenticator",
    "BuildOwner",
    "ChainAuthenticator",
    "MungeAuthenticator",
    "MungeIdentity",
    "MungeVerifier",
    "Operation",
    "PermissionDeniedError",
    "Principal",
    "UnmungeVerifier",
    "authorize",
    "user_of_uid",
]

_log = get_logger(__name__)

Operation = Literal["read", "cache_write", "build_write"]
"""What a route does: read shared state, write the action cache or access times, or change a
build (create it, its actions, results, events or status)."""


class AuthenticationError(MetadataError):
    """Missing or invalid credentials (HTTP 401)."""


class PermissionDeniedError(MetadataError):
    """An authenticated principal may not do this (HTTP 403)."""


@dataclass(frozen=True)
class Principal:
    """Who is calling: a Unix user proven by MUNGE, or the holder of a bearer token."""

    name: str
    via: Literal["munge", "token"]
    token_kind: TokenKind | None = None
    team: str | None = None


@dataclass(frozen=True)
class BuildOwner:
    """The immutable ownership fields of a build (`build` is None while it is being created)."""

    user_name: str
    project: str
    domain: str
    build: int | None = None


class Authenticator(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> Principal | None:
        """The caller, or None if the request carries no credential of this scheme. A
        credential of this scheme that does not verify raises AuthenticationError."""
        ...


class AuthorizeHook(Protocol):
    def __call__(
        self,
        principal: Principal,
        op: Operation,
        *,
        domain: str | None,
        owner: BuildOwner | None,
    ) -> None: ...


def authorize(
    principal: Principal, op: Operation, *, domain: str | None, owner: BuildOwner | None
) -> None:
    """Raise PermissionDeniedError unless `principal` may do `op` (R4).

    Only build writes are restricted in P1: the owner may change a build, and a CI token may
    change builds of its team's project (`team == project`); service tokens never change builds.
    P2-01 adds domain membership for every operation here.
    """
    if op != "build_write":
        return
    if owner is None:  # pragma: no cover - routes always pass the owner of a build write
        raise PermissionDeniedError("build write without a build owner; this is a bug")
    if principal.token_kind != "service" and (
        principal.name == owner.user_name
        or (principal.token_kind == "ci" and principal.team == owner.project)
    ):
        return
    target = (
        f"create a build for user {owner.user_name!r}"
        if owner.build is None
        else f"change build {owner.build} (owned by {owner.user_name!r}, project {owner.project!r})"
    )
    raise PermissionDeniedError(
        f"{principal.name!r} is not allowed to {target}: only the owner, or a CI token of "
        "the build's project, may change a build"
    )


# --- MUNGE ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MungeIdentity:
    uid: int
    gid: int


class MungeVerifier(Protocol):
    def verify(self, credential: str) -> MungeIdentity:
        """The identity a credential proves; AuthenticationError if it does not verify."""
        ...


class _Run(Protocol):
    def __call__(
        self, argv: Sequence[str], *, input: str, timeout: float
    ) -> subprocess.CompletedProcess[str]: ...


def _run(argv: Sequence[str], *, input: str, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # argv list, no shell; the credential goes on stdin
        list(argv), input=input, capture_output=True, text=True, timeout=timeout, check=False
    )


_FIELD: Final = re.compile(r"^([A-Z_]+):\s*(.*?)\s*$", re.MULTILINE)
_NUMBER: Final = re.compile(r"(\d+)\)?$")


class UnmungeVerifier:
    """Decode a credential with `unmunge --numeric` (munged keeps the replay cache)."""

    def __init__(self, unmunge: str = "unmunge", *, run: _Run = _run, timeout_s: float = 5.0):
        self._argv = [unmunge, "--numeric"]
        self._run = run
        self._timeout_s = timeout_s

    def verify(self, credential: str) -> MungeIdentity:
        try:
            done = self._run(self._argv, input=credential, timeout=self._timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            _log.error("unmunge failed to run", error=str(exc))
            raise AuthenticationError(
                f"MUNGE credential could not be checked: {self._argv[0]!r} failed to run"
            ) from exc
        fields = dict(_FIELD.findall(done.stdout))
        status = fields.get("STATUS", "")
        if done.returncode != 0 or not status.startswith("Success"):
            reason = status.split(" (")[0] if status else done.stderr.strip() or "unknown error"
            raise AuthenticationError(f"MUNGE credential refused by unmunge: {reason}")
        try:
            uid = int(_parse_number(fields["UID"]))
            gid = int(_parse_number(fields["GID"]))
        except (KeyError, ValueError):
            raise AuthenticationError(
                "MUNGE credential refused: unmunge printed no UID/GID"
            ) from None
        return MungeIdentity(uid=uid, gid=gid)


def _parse_number(value: str) -> str:
    match = _NUMBER.search(value)
    if match is None:
        raise ValueError(value)
    return match.group(1)


def user_of_uid(uid: int) -> str:
    """The NSS user name of `uid` (LDAP/SSSD on the service host)."""
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        raise AuthenticationError(
            f"MUNGE credential of uid {uid}, which has no user name"
        ) from None


class MungeAuthenticator:
    def __init__(self, verifier: MungeVerifier, users: Callable[[int], str] = user_of_uid) -> None:
        self._verifier = verifier
        self._users = users

    def authenticate(self, headers: Mapping[str, str]) -> Principal | None:
        credential = headers.get(MUNGE_HEADER.lower()) or headers.get(MUNGE_HEADER)
        if credential is None:
            return None
        identity = self._verifier.verify(credential.strip())
        return Principal(name=self._users(identity.uid), via="munge")


# --- bearer tokens --------------------------------------------------------------------------------


class BearerAuthenticator:
    def __init__(self, tokens: TokenStore) -> None:
        self._tokens = tokens

    def authenticate(self, headers: Mapping[str, str]) -> Principal | None:
        value = headers.get("authorization") or headers.get("Authorization")
        if value is None:
            return None
        scheme, _, token = value.strip().partition(" ")
        if scheme.lower() != "bearer":
            return None
        token = token.strip()
        if not token:
            raise AuthenticationError("empty bearer token")
        info = self._tokens.lookup(hash_token(token))
        if info is None:
            raise AuthenticationError("bearer token unknown, revoked or expired")
        return Principal(name=info.principal, via="token", token_kind=info.kind, team=info.team)


class ChainAuthenticator:
    """The first scheme whose credential the request carries decides."""

    def __init__(self, *authenticators: Authenticator) -> None:
        self._authenticators = authenticators

    def authenticate(self, headers: Mapping[str, str]) -> Principal:
        for authenticator in self._authenticators:
            principal = authenticator.authenticate(headers)
            if principal is not None:
                return principal
        raise AuthenticationError(
            f"no credentials: send {MUNGE_HEADER} (the output of `munge -n`) or "
            "'Authorization: Bearer <token>'"
        )
