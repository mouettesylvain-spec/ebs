"""GitLab OIDC login for the dashboard (P2-05 R1) against a fake identity provider.

`FakeIdP` serves discovery, the token endpoint (which checks the client secret and the PKCE
verifier against the challenge of the authorization request) and userinfo, through
`httpx.MockTransport`: no network. Its "authorize" step is the browser's visit to GitLab.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest

from ebs.core.clock import FakeClock
from ebs.core.digest import hash_bytes
from ebs.core.errors import ConfigError
from ebs.meta.api import API_PREFIX, BuildCreate
from ebs.meta.memory import InMemoryMetadataStore
from ebs.meta.service.app import ServiceConfig
from ebs.meta.service.auth import AuthenticationError
from ebs.meta.service.oidc import CSRF_HEADER, SESSION_COOKIE, OidcConfig, OidcLogin
from tests.helpers.meta_http import Service, make_service

ISSUER = "https://gitlab.test"
CLIENT_ID = "ebs-dashboard"
SECRET = "client-secret"
REDIRECT = "https://meta.test/auth/callback"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FakeIdP:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.codes: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, Any] = {}  # overrides of the ID token claims
        self.userinfo: dict[str, Any] = {"sub": "42", "preferred_username": "alice"}
        self.token_requests: list[dict[str, str]] = []

    def authorize(self, location: str) -> tuple[str, str]:
        """The user logs in at GitLab, which redirects back with a code; (code, state)."""
        url = urlsplit(location)
        assert f"{url.scheme}://{url.netloc}{url.path}" == f"{ISSUER}/oauth/authorize"
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert q["response_type"] == "code"
        assert q["client_id"] == CLIENT_ID
        assert q["redirect_uri"] == REDIRECT
        assert "openid" in q["scope"].split()
        assert q["code_challenge_method"] == "S256"
        assert len(q["code_challenge"]) == 43
        code = secrets.token_urlsafe(8)
        self.codes[code] = q
        return code, q["state"]

    def id_token(self, request: dict[str, Any]) -> str:
        now = int(self.clock.now().timestamp())
        claims = {
            "iss": ISSUER, "sub": "42", "aud": CLIENT_ID, "iat": now, "exp": now + 120,
            "nonce": request["nonce"], "preferred_username": "alice",
        } | self.claims  # fmt: skip
        claims = {k: v for k, v in claims.items() if v is not None}
        header = _b64(json.dumps({"alg": "RS256", "kid": "k1"}).encode())
        return f"{header}.{_b64(json.dumps(claims).encode())}.{_b64(b'signature')}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/oauth/authorize",
                "token_endpoint": f"{ISSUER}/oauth/token",
                "userinfo_endpoint": f"{ISSUER}/oauth/userinfo",
            })  # fmt: skip
        if path == "/oauth/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            grant = self.codes.pop(form.get("code", ""), None)
            if (
                grant is None
                or form.get("client_secret") != SECRET
                or form.get("client_id") != CLIENT_ID
                or form.get("redirect_uri") != REDIRECT
                or form.get("grant_type") != "authorization_code"
            ):
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            if _b64(hashlib.sha256(verifier.encode()).digest()) != grant["code_challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "pkce": "mismatch"})
            return httpx.Response(200, json={
                "access_token": "at-1", "token_type": "Bearer", "id_token": self.id_token(grant),
            })  # fmt: skip
        if path == "/oauth/userinfo":
            if request.headers.get("authorization") != "Bearer at-1":
                return httpx.Response(401)
            return httpx.Response(200, json=self.userinfo)
        return httpx.Response(404)


class Site:
    def __init__(self, tmp_path: Path, **config: Any) -> None:
        self.clock = FakeClock()
        self.idp = FakeIdP(self.clock)
        (tmp_path / "client_secret").write_text(SECRET + "\n")
        keys = tmp_path / "session_keys"
        keys.write_text(config.pop("keys", "new-session-key-0123456789abcdef\n"))
        self.config = OidcConfig.model_validate({
            "issuer": ISSUER, "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
            "client_secret_file": str(tmp_path / "client_secret"),
            "session_secret_file": str(keys), "cookie_secure": False,
        } | config)  # fmt: skip
        http = httpx.Client(transport=httpx.MockTransport(self.idp.handle))
        self.login = OidcLogin.from_config(self.config, http=http, clock=self.clock)
        store = InMemoryMetadataStore(clock=self.clock)
        self.svc: Service = make_service(store, oidc=self.login)

    def start(self, next_: str = "/dashboard/builds") -> httpx.Response:
        r: httpx.Response = self.svc.client.get(
            "/auth/login", params={"next": next_}, follow_redirects=False
        )
        assert r.status_code == 302, r.text
        return r

    def log_in(self, next_: str = "/dashboard/builds") -> httpx.Response:
        code, state = self.idp.authorize(self.start(next_).headers["location"])
        return self.callback(code, state)

    def callback(self, code: str, state: str) -> httpx.Response:
        r: httpx.Response = self.svc.client.get(
            "/auth/callback", params={"code": code, "state": state}, follow_redirects=False
        )
        return r

    def whoami(self, **kwargs: Any) -> httpx.Response:
        r: httpx.Response = self.svc.client.get(f"{API_PREFIX}/whoami", **kwargs)
        return r


# R1: authorization code + PKCE: the IdP sees a S256 challenge, then the matching verifier and
# the client secret; the session maps the configured claim to the Unix user name.
def test_login_round_trip(tmp_path: Path) -> None:
    site = Site(tmp_path)
    r = site.log_in()
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/dashboard/builds"
    form = site.idp.token_requests[-1]
    assert form["code_verifier"]
    assert len(form["code_verifier"]) >= 43
    me = site.whoami()
    assert me.status_code == 200, me.text
    assert me.json() == {"name": "alice", "via": "session", "token_kind": None, "team": None}
    assert site.svc.client.cookies.get("ebs_csrf")


# R1: the session and login cookies are HttpOnly, SameSite=Lax and Secure by default.
def test_cookie_flags(tmp_path: Path) -> None:
    site = Site(tmp_path, cookie_secure=True)
    login = site.start().headers["set-cookie"].lower()
    for flag in ("httponly", "samesite=lax", "secure", "path=/auth"):
        assert flag in login, login


# R1: a callback whose state is not the one of this browser's login is refused (login CSRF).
def test_state_must_match(tmp_path: Path) -> None:
    site = Site(tmp_path)
    code, _state = site.idp.authorize(site.start().headers["location"])
    r = site.callback(code, "forged-state")
    assert r.status_code == 401
    assert "state" in r.json()["detail"]
    other = Site(tmp_path)
    code, state = other.idp.authorize(other.start().headers["location"])
    other.svc.client.cookies.clear()  # another browser: no login cookie
    r = other.callback(code, state)
    assert r.status_code == 401
    assert "login" in r.json()["detail"]
    assert SESSION_COOKIE not in other.svc.client.cookies


# R1: the ID token must be for this client, from this issuer, unexpired, with our nonce.
@pytest.mark.parametrize(
    ("claims", "message"),
    [
        ({"iss": "https://evil.test"}, "issuer"),
        ({"aud": "someone-else"}, "audience"),
        ({"aud": ["someone-else", CLIENT_ID], "azp": "someone-else"}, "azp"),
        ({"exp": 1}, "expired"),
        ({"nonce": "replayed"}, "nonce"),
    ],
)
def test_id_token_checks(tmp_path: Path, claims: dict[str, Any], message: str) -> None:
    site = Site(tmp_path)
    site.idp.claims = claims
    r = site.log_in()
    assert r.status_code == 401, r.text
    assert message in r.json()["detail"]
    assert SESSION_COOKIE not in site.svc.client.cookies


# R1: the user name comes from the configured claim, from userinfo when the ID token lacks it;
# userinfo of another subject or a name that is not a Unix user name is refused.
def test_username_claim(tmp_path: Path) -> None:
    site = Site(tmp_path, username_claim="nickname")
    site.idp.userinfo = {"sub": "42", "nickname": "bob"}
    assert site.log_in().status_code == 303
    assert site.whoami().json()["name"] == "bob"

    site = Site(tmp_path, username_claim="nickname")
    site.idp.claims = {"nickname": "carol"}
    assert site.log_in().status_code == 303
    assert site.whoami().json()["name"] == "carol"  # from the ID token itself

    site = Site(tmp_path, username_claim="nickname")
    site.idp.userinfo = {"sub": "43", "nickname": "bob"}
    r = site.log_in()
    assert r.status_code == 401
    assert "sub" in r.json()["detail"]

    site = Site(tmp_path)
    site.idp.claims = {"preferred_username": "Robert'); DROP"}
    r = site.log_in()
    assert r.status_code == 401
    assert "Unix user name" in r.json()["detail"]


# R1: only relative targets after login: no open redirect.
@pytest.mark.parametrize("target", ["https://evil.test/", "//evil.test/x", "/\\evil.test", "x"])
def test_no_open_redirect(tmp_path: Path, target: str) -> None:
    r = Site(tmp_path).log_in(target)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# R1: a tampered or expired session cookie authenticates nobody; a key listed after the
# signing key still verifies (rotation).
def test_session_cookie_integrity(tmp_path: Path) -> None:
    site = Site(tmp_path)
    site.log_in()
    value = site.svc.client.cookies[SESSION_COOKIE]
    body, _, mac = value.rpartition(".")
    forged = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    forged["user"] = "root"
    tampered = _b64(json.dumps(forged).encode()) + "." + mac
    site.svc.client.cookies.clear()
    r = site.whoami(headers={"Cookie": f"{SESSION_COOKIE}={tampered}"})
    assert r.status_code == 401
    assert "session" in r.json()["detail"]
    assert site.whoami(headers={"Cookie": f"{SESSION_COOKIE}={value}"}).status_code == 200
    rotated = Site(
        tmp_path, keys="newer-key-fedcba9876543210fedcba\nnew-session-key-0123456789abcdef\n"
    )
    assert rotated.whoami(headers={"Cookie": f"{SESSION_COOKIE}={value}"}).status_code == 200
    site.clock.advance(site.config.session_ttl_s + 1)
    r = site.whoami(headers={"Cookie": f"{SESSION_COOKIE}={value}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


# R1: mutating browser requests need the session's CSRF token in X-CSRF-Token; reads do not.
def test_csrf_on_mutating_routes(tmp_path: Path) -> None:
    site = Site(tmp_path)
    site.log_in()
    spec = BuildCreate(
        uuid=uuid4(), domain="test", project="demo", plan_digest=hash_bytes(b"p"), flow_repo="",
        flow_commit="", flow_dirty=False, user_name="alice", cache_mode="write",
    ).model_dump_json()  # fmt: skip
    url = f"{API_PREFIX}/builds"
    headers = {"Content-Type": "application/json"}
    r = site.svc.client.post(url, content=spec, headers=headers)
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]
    r = site.svc.client.post(url, content=spec, headers=headers | {CSRF_HEADER: "guess"})
    assert r.status_code == 403
    token = site.svc.client.cookies["ebs_csrf"]
    r = site.svc.client.post(url, content=spec, headers=headers | {CSRF_HEADER: token})
    assert r.status_code == 200, r.text
    assert site.whoami().status_code == 200  # GET: no token needed


# R1: logout needs the CSRF token and clears the session.
def test_logout(tmp_path: Path) -> None:
    site = Site(tmp_path)
    site.log_in()
    assert site.svc.client.post("/auth/logout").status_code == 403
    token = site.svc.client.cookies["ebs_csrf"]
    r = site.svc.client.post("/auth/logout", headers={CSRF_HEADER: token})
    assert r.status_code == 204
    assert SESSION_COOKIE not in site.svc.client.cookies
    assert site.whoami().status_code == 401


# R1: the ID token is trusted because it comes over TLS: http is refused, in the config and in
# the provider's discovery document, unless explicitly allowed (tests only).
def test_https_required(tmp_path: Path) -> None:
    site = Site(tmp_path)
    fields = site.config.model_dump() | {"issuer": "http://gitlab.test"}
    with pytest.raises(ValueError, match="issuer must be an https URL"):
        OidcConfig.model_validate(fields)
    assert OidcConfig.model_validate(fields | {"allow_insecure_http": True}).issuer
    handle = site.idp.handle

    def insecure(request: httpx.Request) -> httpx.Response:
        r = handle(request)
        if request.url.path == "/.well-known/openid-configuration":
            doc = r.json() | {"token_endpoint": "http://gitlab.test/oauth/token"}
            return httpx.Response(200, json=doc)
        return r

    http = httpx.Client(transport=httpx.MockTransport(insecure))
    login = OidcLogin.from_config(site.config, http=http, clock=site.clock)
    with pytest.raises(AuthenticationError, match="not https"):
        login.start("/")


# R1: the login cookie goes back only to the callback's directory, also below a proxy prefix.
def test_login_cookie_path(tmp_path: Path) -> None:
    site = Site(tmp_path, redirect_uri="https://meta.test/ebs/auth/callback")
    assert site.config.login_cookie_path == "/ebs/auth"
    assert "path=/ebs/auth" in site.start().headers["set-cookie"].lower()


# R1: [meta_service.oidc] is validated, and its secret files must exist.
def test_oidc_config(tmp_path: Path) -> None:
    site = Site(tmp_path)
    cfg = ServiceConfig.from_mapping({"oidc": site.config.model_dump()})
    assert cfg.oidc == site.config
    assert cfg.oidc.username_claim == "preferred_username"
    with pytest.raises(ConfigError, match="client_secret_file"):
        OidcLogin.from_config(site.config.model_copy(update={"client_secret_file": "/nope"}))
    (tmp_path / "short").write_text("tiny\n")
    with pytest.raises(ConfigError, match="at least 32"):
        OidcLogin.from_config(
            site.config.model_copy(update={"session_secret_file": str(tmp_path / "short")})
        )
    with pytest.raises(ConfigError, match="oidc"):
        ServiceConfig.from_mapping({"oidc": {"issuer": "gitlab"}})
