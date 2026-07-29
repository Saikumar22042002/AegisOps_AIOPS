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


def _posture(settings: Settings) -> tuple[bool, str | None]:
    """(enabled, blocking_reason). The reason names the ACTUAL blocker.

    "Not enabled" has two causes that need different operator actions, so they are reported
    separately: the flag is off, or the flag is on but no bot token is configured. Collapsing
    them into one message sends the operator to fix the wrong thing.

    The token itself is never returned by any route — only whether one exists.
    """
    if settings.aegisops_telegram != "on":
        return False, "flag_off"
    if not settings.telegram_bot_token:
        return False, "no_token"
    return True, None


def _enabled(settings: Settings) -> bool:
    return _posture(settings)[0]


@router.get("/telegram")
async def telegram_status(user: User = Depends(get_current_user),
                          settings: Settings = Depends(get_settings)) -> dict:
    """The panel's state. `enabled` is false when the operator hasn't turned the gateway on, so
    the UI can say so instead of offering a control that cannot work."""
    org_id = await _org_id(user)
    st = await identity.status(org_id, user.user_id or "", channel=CHANNEL)
    enabled, reason = _posture(settings)
    gw = None
    try:
        from ..gateways.telegram import poller
        live = poller.current()
        gw = live.bot_username if live else None
    except Exception:  # noqa: BLE001 — a status read must never fail the panel
        gw = None
    # Deliberately NOT reported: "is a poller listening". It can only be answered for THIS
    # worker, and the production posture runs several — exactly one wins the getUpdates race
    # (the others log Conflict once and stay idle by design), so a per-worker answer would tell
    # a user "the bot isn't listening" while it is perfectly reachable. `bot_username` is
    # present when this worker built a gateway; the authoritative reachability signal is the
    # startup log, not a field that looks like it answers more than it can.
    return {**st, "enabled": enabled, "reason": reason, "bot_username": gw}


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
