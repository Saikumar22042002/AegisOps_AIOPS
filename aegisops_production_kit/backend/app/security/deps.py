"""Auth FastAPI dependencies — current user resolution + RBAC guards.

Kept separate from both `rbac.py` (pure logic) and `api/auth.py` (the router) so the
guards can be imported by any router without an import cycle.
"""

from __future__ import annotations

import time

from fastapi import Cookie, Depends, HTTPException, status

from ..integrations.keycloak import AuthError, get_oidc
from ..logging_conf import bind_correlation
from ..schemas.auth import User
from ..settings import Settings, get_settings
from . import rbac, sessions

COOKIE_NAME = "aegis_session"


def user_from_claims(claims: dict) -> User:
    realm_roles = claims.get("realm_access", {}).get("roles", [])
    roles = [r for r in realm_roles if r in rbac.ALL_ROLES]
    return User(
        sub=claims.get("sub", ""),
        username=claims.get("preferred_username") or claims.get("email") or claims.get("sub", ""),
        email=claims.get("email"),
        name=claims.get("name") or claims.get("preferred_username"),
        roles=roles,
        display_roles=rbac.display_roles(roles),
        can_approve=rbac.can_approve(roles),
        can_initiate=rbac.can_initiate(roles),
        can_execute=rbac.can_execute(roles),
        org=claims.get("org"),
    )


async def get_current_user(
    aegis_session: str | None = Cookie(default=None),
    settings: Settings = Depends(get_settings),
) -> User:
    """Resolve the authenticated user from the session cookie, refreshing if needed."""
    if not aegis_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    sess = await sessions.get_session(aegis_session)
    if not sess:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    oidc = get_oidc(settings)
    # Refresh proactively when the access token is within 30s of expiry.
    if time.time() >= float(sess.get("expires_at", 0)) - 30 and sess.get("refresh_token"):
        try:
            tokens = await oidc.refresh(sess["refresh_token"])
            sess["access_token"] = tokens.access_token
            sess["refresh_token"] = tokens.refresh_token
            sess["expires_at"] = tokens.expires_at
            await sessions.update_session(aegis_session, sess)
        except AuthError:
            await sessions.delete_session(aegis_session)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired") from None

    try:
        claims = await oidc.validate(sess["access_token"])
    except AuthError:
        await sessions.delete_session(aegis_session)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session") from None

    user = user_from_claims(claims)
    bind_correlation(user=user.username)
    return user


def require_auth(user: User = Depends(get_current_user)) -> User:
    return user


def require_roles(*allowed: str):
    """Dependency factory: caller must hold at least one of the given realm roles."""

    def _dep(user: User = Depends(get_current_user)) -> User:
        if not any(r in allowed for r in user.roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires one of roles: {', '.join(sorted(allowed))}",
            )
        return user

    return _dep


def require_approver(user: User = Depends(get_current_user)) -> User:
    if not user.can_approve:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Approval requires Cloud Architect, Org Admin, or Platform Admin.",
        )
    return user


def require_initiator(user: User = Depends(get_current_user)) -> User:
    if not user.can_initiate:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Read-only roles cannot initiate workflows.",
        )
    return user
