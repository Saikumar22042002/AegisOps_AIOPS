"""GW-1: per-user channel linking endpoints (the Settings → Connected accounts panel).

    GET    /gateways/telegram          link status + whether a code is live (never the code)
    POST   /gateways/telegram/code     issue a one-time code (returned exactly once)
    DELETE /gateways/telegram          unlink this chat account

Every route is the *web-authenticated* user acting on their OWN identity: `get_current_user`
resolves the principal, `repo.org_for` scopes it (S0), and the code is issued for that user
only. There is no route that links someone else's account, and no route that returns an
already-issued code a second time — the plaintext exists only in the HTTP response that
created it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..db import repositories as repo
from ..db.session import session_scope
from ..gateways import identity
from ..logging_conf import get_logger
from ..schemas.auth import User
from ..security.deps import get_current_user
from ..settings import Settings, get_settings

log = get_logger(__name__)
router = APIRouter(tags=["gateways"], prefix="/gateways")

CHANNEL = identity.TELEGRAM


async def _org_id(user: User) -> str:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        return str(org.id)


def _enabled(settings: Settings) -> bool:
    return settings.aegisops_telegram == "on" and bool(settings.telegram_bot_token)


@router.get("/telegram")
async def telegram_status(user: User = Depends(get_current_user),
                          settings: Settings = Depends(get_settings)) -> dict:
    """The panel's state. `enabled` is false when the operator hasn't turned the gateway on, so
    the UI can say so instead of offering a control that cannot work."""
    org_id = await _org_id(user)
    st = await identity.status(org_id, user.user_id or "", channel=CHANNEL)
    gw = None
    try:
        from ..gateways.telegram import poller
        live = poller.current()
        gw = live.bot_username if live else None
    except Exception:  # noqa: BLE001 — a status read must never fail the panel
        gw = None
    return {**st, "enabled": _enabled(settings), "bot_username": gw}


@router.post("/telegram/code")
async def telegram_link_code(user: User = Depends(get_current_user),
                             settings: Settings = Depends(get_settings)) -> dict:
    """Issue a one-time link code for the CALLING user. Returned once, never re-servable."""
    if not _enabled(settings):
        raise HTTPException(400, "The Telegram gateway is not enabled on this deployment "
                                 "(set AEGISOPS_TELEGRAM=on and TELEGRAM_BOT_TOKEN).")
    if not user.user_id:
        raise HTTPException(403, "Your account is not resolved to a platform user yet — "
                                "sign out and back in, then try again.")
    org_id = await _org_id(user)
    try:
        code, expires_at = await identity.generate_code(
            org_id, user.user_id, channel=CHANNEL, issued_by=user.username,
            ttl_seconds=settings.gateway_link_code_ttl_seconds)
    except identity.LinkError as exc:
        raise HTTPException(400, str(exc)) from None
    bot = None
    try:
        from ..gateways.telegram import poller
        live = poller.current()
        bot = live.bot_username if live else None
    except Exception:  # noqa: BLE001
        bot = None
    return {"code": code, "expires_at": expires_at.isoformat(),
            "ttl_seconds": settings.gateway_link_code_ttl_seconds,
            "bot_username": bot,
            "instructions": f"Send `/link {code}` to the AegisOps bot on Telegram"
                            + (f" (@{bot})" if bot else "")}


@router.delete("/telegram")
async def telegram_unlink(user: User = Depends(get_current_user)) -> dict:
    """Cut this user's Telegram binding. Audited; idempotent."""
    org_id = await _org_id(user)
    if not user.user_id:
        raise HTTPException(403, "Your account is not resolved to a platform user yet.")
    removed = await identity.unlink(org_id, user.user_id, channel=CHANNEL, actor=user.username)
    return {"status": "unlinked" if removed else "not_linked", "channel": CHANNEL}
