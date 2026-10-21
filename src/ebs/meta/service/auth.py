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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, Protocol

from ebs.core.errors import MetadataError
from ebs.core.log import get_logger
from ebs.meta.client import MUNGE_HEADER, munge_payload
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
    "NotFoundError",
    "Operation",
    "PermissionDeniedError",
    "Policy",
    "Principal",
    "RequestInfo",
    "UnmungeVerifier",
    "authorize",
    "user_of_uid",
]

_log = get_logger(__name__)

Operation = Literal[
    "read",
    "cache_write",
    "build_write",
    "toolchain_write",
    "release_write",
    "release_export",
    "queue_write",
]
"""What a route does: read shared state, write the action cache or access times, change a
build (create it, its actions, results, events or status), register a toolchain, create, yank
or move the channel of a release of a build (P2-02), export a release to another domain, or
queue, lease and finish the actions of a pilot pool (P3-06)."""


class AuthenticationError(MetadataError):
    """Missing or invalid credentials (HTTP 401)."""


class PermissionDeniedError(MetadataError):
    """An authenticated principal may not do this (HTTP 403)."""


class NotFoundError(MetadataError):
    """No such object, or one the caller may not know about (HTTP 404, P2-01 R2)."""


@dataclass(frozen=True)
class Principal:
    """Who is calling: a Unix user proven by MUNGE, the holder of a bearer token, or a
    dashboard user logged in through OIDC (`session`, P2-05), named by their Unix user name.

    `authorized` records the operations authorized during the request (`ServiceDeps.check`); the
    service refuses to send a success that none was (fail closed, P2-01 R1)."""

    name: str
    via: Literal["munge", "token", "session"]
    token_kind: TokenKind | None = None
    team: str | None = None
    authorized: list[str] = field(default_factory=list, compare=False, repr=False, hash=False)


@dataclass(frozen=True)
class BuildOwner:
    """The immutable ownership fields of a build (`build` is None while it is being created)."""

    user_name: str
    project: str
    domain: str
    build: int | None = None


@dataclass(frozen=True)
class RequestInfo:
    """What authenticators see of a request: `path` is below `API_PREFIX`, percent-decoded."""

    method: str
    path: str
    headers: Mapping[str, str]


class Authenticator(Protocol):
    def authenticate(self, request: RequestInfo) -> Principal | None:
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


class Policy:
    """The default `authorize()` hook (R4); `toolchain_admins` may register toolchains.

    - `build_write`: the build's owner, or a `ci` token whose team is the build's project.
      Service tokens never change builds.
    - `toolchain_write` (P1-07 registry): `service` tokens and the user names in
      `toolchain_admins` (`[meta_service].toolchain_admins`). A new current toolchain changes
      the toolchain of every later build, so ordinary users may not register one.
    - `release_write`, `release_export` (P2-02): like `build_write` on the release's build (its
      owner, or a CI token of its project). P2-01 adds team and domain membership (an export
      needs both domains; `domain` is the target).
    - `read`, `cache_write`, `queue_write` (P3-06 pilot queues): any authenticated principal.
      P2-01 adds domain membership here.
    """

    def __init__(self, toolchain_admins: Iterable[str] = ()) -> None:
        self.toolchain_admins = frozenset(toolchain_admins)

    def __call__(
        self,
        principal: Principal,
        op: Operation,
        *,
        domain: str | None,
        owner: BuildOwner | None,
    ) -> None:
        if op == "toolchain_write":
            if principal.token_kind == "service" or (
                principal.token_kind is None and principal.name in self.toolchain_admins
            ):
                return
            raise PermissionDeniedError(
                f"{principal.name!r} is not allowed to register toolchains: only service tokens "
                "and the users in [meta_service].toolchain_admins may"
            )
        if op not in ("build_write", "release_write", "release_export"):
            return
        if owner is None:  # pragma: no cover - routes always pass the owner of a build write
            raise PermissionDeniedError("build write without a build owner; this is a bug")
        if principal.token_kind != "service" and (
            principal.name == owner.user_name
            or (principal.token_kind == "ci" and principal.team == owner.project)
        ):
            return
        of_build = f"build {owner.build} (owned by {owner.user_name!r}, project {owner.project!r})"
        if op != "build_write":
            raise PermissionDeniedError(
                f"{principal.name!r} is not allowed to release, yank or export outputs of "
                f"{of_build}: only the build's owner, or a CI token of its project, may"
            )
        target = (
            f"create a build for user {owner.user_name!r}"
            if owner.build is None
            else f"change {of_build}"
        )
        raise PermissionDeniedError(
            f"{principal.name!r} is not allowed to {target}: only the owner, or a CI token of "
            "the build's project, may change a build"
        )


authorize: AuthorizeHook = Policy()
"""The default policy without toolchain admins (`app_from_config` passes the configured ones)."""


# --- MUNGE ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MungeIdentity:
    uid: int
    gid: int
    payload: str = ""


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


_FIELD: Final = re.compile(r"^([A-Z_]+):\s*(.*?)\s*$")
_NUMBER: Final = re.compile(r"(\d+)\)?$")


def _decode_output(stdout: str) -> tuple[dict[str, str], str]:
    """unmunge's metadata (`KEY: value` lines up to the first blank line) and the payload that
    follows it.

    Only the metadata block is parsed for fields: the payload is chosen by the caller and could
    imitate metadata lines. A repeated key means the output is not what unmunge prints.
    """
    fields: dict[str, str] = {}
    lines = stdout.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            return fields, "\n".join(lines[i + 1 :])
        match = _FIELD.match(line)
        if match is None:
            continue
        key, value = match.groups()
        if key in fields:
            raise AuthenticationError(f"MUNGE credential refused: unmunge printed {key} twice")
        fields[key] = value
    return fields, ""


class UnmungeVerifier:
    """Decode a credential with `unmunge --numeric` (munged keeps the replay cache).

    unmunge prints the metadata, a blank line, then the payload; exactly `LENGTH` characters
    of it are returned as `MungeIdentity.payload` for the authenticator to check.
    """

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
        fields, rest = _decode_output(done.stdout)
        status = fields.get("STATUS", "")
        if done.returncode != 0 or not status.startswith("Success"):
            reason = status.split(" (")[0] if status else done.stderr.strip() or "unknown error"
            raise AuthenticationError(f"MUNGE credential refused by unmunge: {reason}")
        try:
            uid = int(_parse_number(fields["UID"]))
            gid = int(_parse_number(fields["GID"]))
            length = int(fields["LENGTH"])
        except (KeyError, ValueError):
            raise AuthenticationError(
                "MUNGE credential refused: unmunge printed no UID, GID or LENGTH"
            ) from None
        if length < 0 or len(rest) < length:
            raise AuthenticationError("MUNGE credential refused: truncated payload")
        return MungeIdentity(uid=uid, gid=gid, payload=rest[:length])


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
    """MUNGE credentials bound to this service and request (`munge_payload`, D1).

    `audience` names this service (`[meta_service].audience`, the `host[:port]` clients use). A
    credential whose payload is not exactly `munge_payload(audience, method, path)` of the
    request is refused: one captured from other MUNGE traffic, or minted for another request.
    """

    def __init__(
        self,
        verifier: MungeVerifier,
        users: Callable[[int], str] = user_of_uid,
        *,
        audience: str,
    ) -> None:
        self._verifier = verifier
        self._users = users
        self.audience = audience

    def authenticate(self, request: RequestInfo) -> Principal | None:
        credential = request.headers.get(MUNGE_HEADER.lower()) or request.headers.get(MUNGE_HEADER)
        if credential is None:
            return None
        identity = self._verifier.verify(credential.strip())
        if identity.payload != munge_payload(self.audience, request.method, request.path):
            raise AuthenticationError(
                f"MUNGE credential refused: it is not bound to this request on "
                f"{self.audience!r}; clients create it with `munge --string "
                f"'{munge_payload(self.audience, request.method, request.path)}'` (check that "
                "[metadata].url or [metadata].audience names this service)"
            )
        return Principal(name=self._users(identity.uid), via="munge")


# --- bearer tokens --------------------------------------------------------------------------------


class BearerAuthenticator:
    def __init__(self, tokens: TokenStore) -> None:
        self._tokens = tokens

    def authenticate(self, request: RequestInfo) -> Principal | None:
        value = request.headers.get("authorization") or request.headers.get("Authorization")
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

    def authenticate(self, request: RequestInfo) -> Principal:
        for authenticator in self._authenticators:
            principal = authenticator.authenticate(request)
            if principal is not None:
                return principal
        raise AuthenticationError(
            f"no credentials: send {MUNGE_HEADER} (a MUNGE credential bound to the request) or "
            "'Authorization: Bearer <token>'"
        )
