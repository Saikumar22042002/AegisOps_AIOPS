"""Authentication endpoints (real Keycloak OIDC).

- POST /auth/login      email/password (Keycloak Direct Access Grant) — powers the design's form.
- GET  /auth/sso/login  Authorization Code + PKCE redirect (the "Continue with Keycloak SSO" button).
- GET  /auth/callback   code exchange → server-side session → redirect to the app.
- GET  /auth/me         current user + roles (401 if unauthenticated).
- POST /auth/logout     revoke refresh token, drop session, clear cookie.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from ..integrations.keycloak import AuthError, KeycloakOIDC, TokenSet, generate_pkce, get_oidc
from ..logging_conf import get_logger
from ..schemas.auth import AuthResponse, LoginRequest, User
from ..security import sessions
from ..security.deps import COOKIE_NAME, get_current_user, user_from_claims
from ..settings import Settings, get_settings

log = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


def _frontend_url(settings: Settings) -> str:
    origins = settings.cors_origin_list
    return origins[0] if origins else "http://localhost:3000"


def _set_session_cookie(response: Response, sid: str, settings: Settings) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="lax",
        max_age=sessions.SESSION_TTL,
        path="/",
    )


async def _establish_session(tokens: TokenSet, oidc: KeycloakOIDC, settings: Settings) -> tuple[str, User]:
    claims = await oidc.validate(tokens.access_token)
    user = user_from_claims(claims)
    sid = await sessions.create_session(
        {
            "user": user.model_dump(),
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "expires_at": tokens.expires_at,
        }
    )
    log.info("auth.session_created", user=user.username, roles=user.roles)
    return sid, user


@router.post("/login", response_model=AuthResponse)
async def login(
    body: LoginRequest, response: Response, settings: Settings = Depends(get_settings)
) -> AuthResponse:
    oidc = get_oidc(settings)
    try:
        tokens = await oidc.password_grant(body.email, body.password)
    except AuthError as exc:
        log.info("auth.login_failed", email=body.email)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials") from exc
    sid, user = await _establish_session(tokens, oidc, settings)
    _set_session_cookie(response, sid, settings)
    return AuthResponse(user=user)


@router.get("/sso/login")
async def sso_login(request: Request, settings: Settings = Depends(get_settings)) -> RedirectResponse:
    oidc = get_oidc(settings)
    verifier, challenge = generate_pkce()
    state = secrets.token_urlsafe(24)
    await sessions.set_oauth_state(state, verifier)
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/callback"
    url = await oidc.build_auth_url(redirect_uri, state, challenge)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code/state")
    verifier = await sessions.pop_oauth_state(state)
    if not verifier:
        raise HTTPException(status_code=400, detail="Invalid or expired state")
    oidc = get_oidc(settings)
    redirect_uri = str(request.base_url).rstrip("/") + "/auth/callback"
    try:
        tokens = await oidc.exchange_code(code, redirect_uri, verifier)
    except AuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Code exchange failed") from exc
    sid, _user = await _establish_session(tokens, oidc, settings)
    redirect = RedirectResponse(_frontend_url(settings), status_code=status.HTTP_302_FOUND)
    _set_session_cookie(redirect, sid, settings)
    return redirect


@router.get("/me", response_model=AuthResponse)
async def me(user: User = Depends(get_current_user)) -> AuthResponse:
    return AuthResponse(user=user)


@router.post("/logout")
async def logout(
    response: Response,
    aegis_session: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if aegis_session:
        sess = await sessions.get_session(aegis_session)
        if sess and sess.get("refresh_token"):
            await get_oidc(settings).logout(sess["refresh_token"])
        await sessions.delete_session(aegis_session)
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "logged_out"}
