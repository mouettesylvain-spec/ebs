"""GitLab OIDC login for the dashboard (P2-05 R1): authorization code flow with PKCE.

    GET  /auth/login?next=/path   redirect to GitLab with state, nonce and an S256 challenge
    GET  /auth/callback           exchange the code (client secret + PKCE verifier), set the session
    POST /auth/logout             clear the session (needs the CSRF token)

The login state (state, nonce, PKCE verifier, target) travels in a short-lived signed cookie
on `/auth`, so the service keeps no login table and any worker can finish a login. The ID token
comes straight from GitLab's token endpoint over TLS, which OIDC Core § 3.1.3.7 accepts in place
of checking its signature; its issuer, audience (and `azp`), expiry and nonce are checked. The
user name is the configured claim (`username_claim`, from the ID token or else from userinfo,
whose `sub` must match) and must be a Unix user name: authorization then uses that user's NSS
groups like a MUNGE caller's.

The session is a signed cookie (HMAC-SHA256, `session_secret_file`: one key per line, the first
signs, all verify, so keys rotate without logging everyone out), HttpOnly, SameSite=Lax and
Secure. It carries a CSRF token, also sent in the readable `ebs_csrf` cookie: every request
with a method other than GET, HEAD or OPTIONS must echo it in `X-CSRF-Token` (double submit),
or it is refused (403). Sessions are stateless: logout clears the cookies, and a copied cookie
stays valid until it expires (`session_ttl_s`) or the keys change.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Annotated, Any, Final
from urllib.parse import urlencode, urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictStr,
    field_validator,
    model_validator,
)

from ebs.core.clock import Clock, SystemClock
from ebs.core.errors import ConfigError
from ebs.core.log import get_logger
from ebs.meta.service.auth import (
    AuthenticationError,
    PermissionDeniedError,
    Principal,
    RequestInfo,
)

__all__ = [
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "LOGIN_COOKIE",
    "SESSION_COOKIE",
    "Login",
    "OidcConfig",
    "OidcLogin",
    "SessionAuthenticator",
    "SessionSigner",
    "safe_next",
]

_log = get_logger(__name__)

SESSION_COOKIE: Final = "ebs_session"
CSRF_COOKIE: Final = "ebs_csrf"
LOGIN_COOKIE: Final = "ebs_login"
CSRF_HEADER: Final = "X-CSRF-Token"
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_UNIX_NAME: Final = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
_MIN_KEY_BYTES: Final = 32
_LEEWAY_S: Final = 60


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class OidcConfig(BaseModel):
    """`[meta_service.oidc]` of the service's ebs.toml."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    issuer: StrictStr  # e.g. https://gitlab.example.com
    client_id: StrictStr
    client_secret_file: StrictStr
    redirect_uri: StrictStr  # https://<service>/auth/callback, registered in GitLab
    session_secret_file: StrictStr
    username_claim: StrictStr = "preferred_username"  # GitLab: the user name
    scopes: list[StrictStr] = Field(default_factory=lambda: ["openid", "profile"])
    session_ttl_s: Annotated[int, Field(ge=60)] = 8 * 3600
    login_ttl_s: Annotated[int, Field(ge=30)] = 600
    cookie_secure: StrictBool = True
    # The ID token's signature is not checked: it is trusted because it comes over TLS from the
    # token endpoint. Plain http is refused unless this is set (tests, never production).
    allow_insecure_http: StrictBool = False

    @model_validator(mode="after")
    def _https(self) -> OidcConfig:
        for name in ("issuer", "redirect_uri"):
            value = getattr(self, name)
            if not self.allow_insecure_http and not value.startswith("https://"):
                raise ValueError(f"{name} must be an https URL (the ID token relies on TLS)")
        return self

    @property
    def callback_path(self) -> str:
        """The path of `redirect_uri` (`/auth/callback`, or below a proxy's prefix)."""
        return urlsplit(self.redirect_uri).path or "/"

    @property
    def login_cookie_path(self) -> str:
        """Where the login cookie is sent back: the directory of the callback."""
        return self.callback_path.rsplit("/", 1)[0] or "/"

    @field_validator("issuer", "redirect_uri")
    @classmethod
    def _url(cls, value: str) -> str:
        if not re.match(r"^https?://[^/\s]+", value):
            raise ValueError(f"expected an http(s) URL, not {value!r}")
        return value.rstrip("/") if value.count("/") == 2 else value

    @field_validator("client_secret_file", "session_secret_file")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError(f"expected an absolute path, not {value!r}")
        return value

    @field_validator("scopes")
    @classmethod
    def _openid(cls, value: list[str]) -> list[str]:
        if "openid" not in value:
            raise ValueError("scopes must include 'openid'")
        return value


class SessionSigner:
    """Signed, expiring JSON values: `base64url(json).base64url(hmac)`."""

    def __init__(self, keys: Sequence[bytes], clock: Clock) -> None:
        if not keys:
            raise ConfigError("no session keys")
        self._keys = list(keys)
        self._clock = clock

    def _mac(self, key: bytes, body: str) -> str:
        return _b64(hmac.new(key, body.encode(), hashlib.sha256).digest())

    def sign(self, doc: Mapping[str, Any], ttl_s: int) -> str:
        exp = int(self._clock.now().timestamp()) + ttl_s
        body = _b64(json.dumps({**doc, "exp": exp}, separators=(",", ":")).encode())
        return f"{body}.{self._mac(self._keys[0], body)}"

    def verify(self, value: str, what: str) -> dict[str, Any]:
        body, _, mac = value.rpartition(".")
        if not body or not any(hmac.compare_digest(mac, self._mac(k, body)) for k in self._keys):
            raise AuthenticationError(f"invalid {what} cookie: log in again (/auth/login)")
        try:
            doc = json.loads(_unb64(body))
        except (binascii.Error, ValueError):
            raise AuthenticationError(f"invalid {what} cookie: log in again") from None
        if not isinstance(doc, dict) or not isinstance(doc.get("exp"), int):
            raise AuthenticationError(f"invalid {what} cookie: log in again")
        if doc["exp"] <= self._clock.now().timestamp():
            raise AuthenticationError(f"{what} expired: log in again (/auth/login)")
        return doc


def cookies_of(headers: Mapping[str, str]) -> dict[str, str]:
    raw = headers.get("cookie") or headers.get("Cookie") or ""
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except CookieError:
        return {}
    return {k: m.value for k, m in jar.items()}


def safe_next(target: str | None) -> str:
    """Where to go after login: a path on this site, else `/` (no open redirect)."""
    if (
        not target
        or not target.startswith("/")
        or target.startswith("//")
        or "\\" in target
        or any(ord(c) < 0x20 for c in target)
    ):
        return "/"
    return target


class SessionAuthenticator:
    """The `ebs_session` cookie as an authentication scheme (`Principal.via == "session"`),
    with the CSRF check of mutating requests."""

    def __init__(self, signer: SessionSigner) -> None:
        self._signer = signer

    def session(self, headers: Mapping[str, str]) -> dict[str, Any] | None:
        value = cookies_of(headers).get(SESSION_COOKIE)
        if value is None:
            return None
        doc = self._signer.verify(value, "session")
        if not isinstance(doc.get("user"), str) or not isinstance(doc.get("csrf"), str):
            raise AuthenticationError("invalid session cookie: log in again")
        return doc

    def check_csrf(self, method: str, headers: Mapping[str, str], doc: Mapping[str, Any]) -> None:
        if method.upper() in _SAFE_METHODS:
            return
        sent = headers.get(CSRF_HEADER.lower()) or headers.get(CSRF_HEADER) or ""
        if not hmac.compare_digest(sent.encode(), str(doc["csrf"]).encode()):
            raise PermissionDeniedError(
                f"CSRF check failed: a browser request that changes state must send the "
                f"{CSRF_COOKIE} cookie's value in {CSRF_HEADER}"
            )

    def authenticate(self, request: RequestInfo) -> Principal | None:
        doc = self.session(request.headers)
        if doc is None:
            return None
        self.check_csrf(request.method, request.headers, doc)
        return Principal(name=doc["user"], via="session")


@dataclass(frozen=True)
class Login:
    """A finished login: the session cookie's value, its CSRF token and where to go."""

    user: str
    session: str
    csrf: str
    next: str


class OidcLogin:
    """The login flow against one OIDC provider; `from_config` reads the secret files."""

    def __init__(
        self,
        config: OidcConfig,
        *,
        client_secret: str,
        session_keys: Sequence[bytes],
        http: httpx.Client | None = None,
        clock: Clock | None = None,
        token: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self.config = config
        self._secret = client_secret
        self._clock = clock or SystemClock()
        self.signer = SessionSigner(session_keys, self._clock)
        self.sessions = SessionAuthenticator(self.signer)
        self._http = http or httpx.Client(timeout=httpx.Timeout(10.0))
        self._token = token
        self._discovery: dict[str, Any] | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_config(
        cls, config: OidcConfig, *, http: httpx.Client | None = None, clock: Clock | None = None
    ) -> OidcLogin:
        secret = _read(config.client_secret_file, "client_secret_file").strip()
        lines = _read(config.session_secret_file, "session_secret_file").splitlines()
        keys = [k.strip().encode() for k in lines if k.strip()]
        if not keys or any(len(k) < _MIN_KEY_BYTES for k in keys):
            raise ConfigError(
                f"[meta_service.oidc].session_secret_file: each key (one per line) must be at "
                f"least {_MIN_KEY_BYTES} characters; create one with `openssl rand -hex 32`"
            )
        return cls(config, client_secret=secret, session_keys=keys, http=http, clock=clock)

    # --- provider ---------------------------------------------------------------------------

    def discovery(self) -> dict[str, Any]:
        with self._lock:
            if self._discovery is not None:
                return self._discovery
        url = f"{self.config.issuer}/.well-known/openid-configuration"
        try:
            r = self._http.get(url)
            r.raise_for_status()
            doc = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            _log.error("OIDC discovery failed", url=url, error=str(exc))
            raise AuthenticationError(
                f"the identity provider is unavailable ({url}); try again later"
            ) from None
        if not isinstance(doc, dict) or doc.get("issuer") != self.config.issuer:
            raise AuthenticationError(
                f"OIDC discovery at {url} names another issuer; check [meta_service.oidc].issuer"
            )
        for field in ("authorization_endpoint", "token_endpoint"):
            if not isinstance(doc.get(field), str):
                raise AuthenticationError(f"OIDC discovery at {url} has no {field}")
        for field in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
            endpoint = doc.get(field)
            if (
                isinstance(endpoint, str)
                and not endpoint.startswith("https://")
                and not self.config.allow_insecure_http
            ):
                raise AuthenticationError(
                    f"OIDC discovery at {url}: {field} {endpoint!r} is not https; the ID token "
                    "is trusted only over TLS"
                )
        with self._lock:
            self._discovery = doc
        return doc

    # --- the flow ---------------------------------------------------------------------------

    def start(self, next_: str | None) -> tuple[str, str]:
        """The authorization URL to redirect to, and the login cookie's value."""
        state, nonce, verifier = self._token(24), self._token(24), self._token(48)
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        query = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": " ".join(self.config.scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        url = f"{self.discovery()['authorization_endpoint']}?{urlencode(query)}"
        login = {"state": state, "nonce": nonce, "verifier": verifier, "next": safe_next(next_)}
        return url, self.signer.sign(login, self.config.login_ttl_s)

    def finish(
        self, *, code: str | None, state: str | None, error: str | None, login_cookie: str | None
    ) -> Login:
        """Check the callback and exchange its code for a session."""
        if login_cookie is None:
            raise AuthenticationError(
                "no login in progress in this browser (the login cookie is missing or expired); "
                "start again at /auth/login"
            )
        login = self.signer.verify(login_cookie, "login")
        if error:
            raise AuthenticationError(f"the identity provider refused the login: {error}")
        if not state or not hmac.compare_digest(state.encode(), str(login.get("state")).encode()):
            raise AuthenticationError("login refused: the state does not match this login")
        if not code:
            raise AuthenticationError("login refused: the callback carries no code")
        tokens = self._exchange(code, str(login["verifier"]))
        claims = self._id_claims(tokens, str(login["nonce"]))
        user = self._username(claims, tokens)
        csrf = self._token(24)
        session = self.signer.sign({"user": user, "csrf": csrf}, self.config.session_ttl_s)
        _log.info("dashboard login", user=user, sub=claims.get("sub"))
        return Login(user=user, session=session, csrf=csrf, next=safe_next(str(login["next"])))

    def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_uri,
            "client_id": self.config.client_id,
            "client_secret": self._secret,
            "code_verifier": verifier,
        }
        try:
            r = self._http.post(str(self.discovery()["token_endpoint"]), data=form)
            doc = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            _log.error("OIDC token exchange failed", error=str(exc))
            raise AuthenticationError("the identity provider is unavailable; try again") from None
        if r.status_code != 200 or not isinstance(doc, dict):
            reason = doc.get("error", r.status_code) if isinstance(doc, dict) else r.status_code
            raise AuthenticationError(f"login refused by the identity provider: {reason}")
        if not isinstance(doc.get("id_token"), str):
            raise AuthenticationError("the identity provider returned no ID token")
        return doc

    def _id_claims(self, tokens: Mapping[str, Any], nonce: str) -> dict[str, Any]:
        parts = str(tokens["id_token"]).split(".")
        try:
            claims = json.loads(_unb64(parts[1])) if len(parts) == 3 else None
        except (binascii.Error, ValueError):
            claims = None
        if not isinstance(claims, dict):
            raise AuthenticationError("login refused: the ID token is malformed")
        if claims.get("iss") != self.config.issuer:
            raise AuthenticationError("login refused: the ID token is from another issuer")
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.config.client_id not in audiences:
            raise AuthenticationError("login refused: the ID token's audience is another client")
        if len(audiences) > 1 and claims.get("azp") != self.config.client_id:
            raise AuthenticationError("login refused: the ID token's azp is another client")
        exp = claims.get("exp")
        if not isinstance(exp, int | float) or exp + _LEEWAY_S <= self._clock.now().timestamp():
            raise AuthenticationError("login refused: the ID token has expired")
        if not hmac.compare_digest(str(claims.get("nonce", "")).encode(), nonce.encode()):
            raise AuthenticationError("login refused: the ID token's nonce is not this login's")
        return claims

    def _username(self, claims: Mapping[str, Any], tokens: Mapping[str, Any]) -> str:
        claim = self.config.username_claim
        name = claims.get(claim)
        if name is None:
            endpoint = self.discovery().get("userinfo_endpoint")
            access = tokens.get("access_token")
            if not isinstance(endpoint, str) or not isinstance(access, str):
                raise AuthenticationError(f"login refused: no {claim!r} claim and no userinfo")
            try:
                r = self._http.get(endpoint, headers={"Authorization": f"Bearer {access}"})
                r.raise_for_status()
                info = r.json()
            except (httpx.HTTPError, ValueError):
                raise AuthenticationError("the identity provider's userinfo failed") from None
            if not isinstance(info, dict) or info.get("sub") != claims.get("sub"):
                raise AuthenticationError("login refused: userinfo is of another sub")
            name = info.get(claim)
        if not isinstance(name, str) or not _UNIX_NAME.fullmatch(name):
            raise AuthenticationError(
                f"login refused: claim {claim!r} ({name!r}) is not a Unix user name; check "
                "[meta_service.oidc].username_claim"
            )
        return name


def _read(path: str, what: str) -> str:
    try:
        return Path(path).read_text()
    except OSError as exc:
        raise ConfigError(f"[meta_service.oidc].{what}: cannot read {path}: {exc}") from None
