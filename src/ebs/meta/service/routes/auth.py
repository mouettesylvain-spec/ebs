"""Browser login routes (P2-05 R1), mounted when `[meta_service.oidc]` is configured.

They are outside the API (no OpenAPI entry) and take no API credentials: `login` and
`callback` run the OIDC flow of `ebs.meta.service.oidc`, `logout` needs the session and its
CSRF token. Failures answer 401 like any authentication failure.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from ebs.meta.service.auth import AuthenticationError, RequestInfo
from ebs.meta.service.oidc import CSRF_COOKIE, LOGIN_COOKIE, SESSION_COOKIE, OidcLogin

router = APIRouter(prefix="/auth", include_in_schema=False)


def _oidc(request: Request) -> OidcLogin:
    oidc: OidcLogin | None = request.app.state.deps.oidc
    if oidc is None:  # pragma: no cover - the router is only mounted with OIDC configured
        raise AuthenticationError("browser login is not configured ([meta_service.oidc])")
    return oidc


@router.get("/login")
def login(request: Request, next: str | None = None) -> Response:
    oidc = _oidc(request)
    url, cookie = oidc.start(next)
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        LOGIN_COOKIE,
        cookie,
        max_age=oidc.config.login_ttl_s,
        path=oidc.config.login_cookie_path,  # only the callback gets it back
        httponly=True,
        secure=oidc.config.cookie_secure,
        samesite="lax",
    )
    return response


@router.get("/callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> Response:
    oidc = _oidc(request)
    done = oidc.finish(
        code=code, state=state, error=error, login_cookie=request.cookies.get(LOGIN_COOKIE)
    )
    response = RedirectResponse(done.next, status_code=303)
    secure, ttl = oidc.config.cookie_secure, oidc.config.session_ttl_s
    response.set_cookie(
        SESSION_COOKIE, done.session, max_age=ttl, path="/", httponly=True, secure=secure,
        samesite="lax",
    )  # fmt: skip
    # Readable by the dashboard's scripts, which echo it in X-CSRF-Token.
    response.set_cookie(
        CSRF_COOKIE, done.csrf, max_age=ttl, path="/", secure=secure, samesite="lax"
    )
    response.delete_cookie(LOGIN_COOKIE, path=oidc.config.login_cookie_path)
    return response


@router.post("/logout", status_code=204)
def logout(request: Request) -> Response:
    oidc = _oidc(request)
    info = RequestInfo(method=request.method, path=request.url.path, headers=request.headers)
    if oidc.sessions.authenticate(info) is None:
        raise AuthenticationError("not logged in")
    response = Response(status_code=204)
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/", secure=oidc.config.cookie_secure, samesite="lax")
    return response
