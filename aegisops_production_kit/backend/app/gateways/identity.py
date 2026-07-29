"""GW-1: channel account ⇄ platform user binding.

The whole point of this module: a messaging channel gives us an opaque account id, and an
opaque account id is **not** an authenticated principal. Nothing on this platform may run
against one. So a web-authenticated user issues a one-time code, sends it from the channel, and
we record a binding. From then on every gateway request resolves to that platform user, and
RBAC / tenancy / four-eyes evaluate against them exactly as they do on the web.

Deliberately NOT adopted from waku's telegram gateway: `TELEGRAM_ALLOWED_USER`. An env-var
allowlist of chat ids answers "may this chat talk to the bot?" — it cannot answer "who is this,
which org are they in, and what are they allowed to do?", which is the only question that
matters here.

Security properties, each enforced below:

* the code is stored **hashed** (SHA-256) — it is a bearer secret typed into a third-party app,
  so a DB read must not yield a live code (`_hash`, `generate_code`);
* **single-use** — `used_at` is stamped inside the same transaction that creates the binding, so
  two concurrent redemptions cannot both win (`consume_code`);
* **expiring** — `expires_at`, default 10 minutes (`settings.gateway_link_code_ttl_seconds`);
* **one issue at a time** — generating a code invalidates the issuer's prior unused codes, so a
  screenshot of an old code is dead (`generate_code`);
* **audited** — link and unlink both write an `audit_log` row (`_audit`).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select

from ..db import repositories as repo
from ..db.models import ChannelIdentity, ChannelLinkCode, Organization, User
from ..db.session import session_scope
from ..schemas.auth import User as AuthUser
from ..security import rbac

log = structlog.get_logger(__name__)

TELEGRAM = "telegram"

# Unambiguous alphabet: no 0/O, 1/I/L — the code is read off a screen and typed on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_LEN = 8


class LinkError(Exception):
    """A link attempt that must be refused, with a message safe to show in the channel."""


@dataclass(frozen=True)
class BoundIdentity:
    """A resolved channel identity: who this chat account is on the platform."""

    identity_id: str
    org_id: str
    user_id: str
    channel: str
    channel_user_id: str
    channel_chat_id: str
    username: str
    email: str | None
    name: str | None
    roles: list[str]
    active_session_id: str | None

    def auth_user(self) -> AuthUser:
        """The same `User` shape every RBAC guard consumes, built from the bound platform user.

        Roles come from the `users` mirror row, which `security.tenancy.resolve_tenancy`
        refreshes from Keycloak on every web login — so a role change takes effect for this
        channel the next time the user signs in to the web app. That is a real bound: a role
        REVOKED in Keycloak is still honoured here only after their next web login, so the
        capability check is re-evaluated on every gateway request (never cached in the chat)
        and the binding can be cut instantly with Unlink.
        """
        return AuthUser(
            sub="",  # no OIDC token in this path; the binding is the credential
            username=self.username,
            email=self.email,
            name=self.name,
            roles=list(self.roles),
            display_roles=rbac.display_roles(self.roles),
            can_approve=rbac.can_approve(self.roles),
            can_initiate=rbac.can_initiate(self.roles),
            can_execute=rbac.can_execute(self.roles),
            org=None,
            org_id=self.org_id,
            user_id=self.user_id,
        )


def _hash(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def _normalize(code: str) -> str:
    """Codes are shown grouped (`ABCD-EFGH`) and retyped by hand — accept either form,
    case-insensitively, and ignore spaces."""
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def _format(code: str) -> str:
    return f"{code[:4]}-{code[4:]}" if len(code) == _CODE_LEN else code


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _audit(s, *, org_id: uuid.UUID, actor: str, action: str, target: str,
                 detail: dict) -> None:
    await repo.AuditRepo.log(s, org_id=org_id, actor=actor, action=action, target=target,
                             detail=detail)


# ── issuing ──────────────────────────────────────────────────────────────────────────────────


async def generate_code(org_id: str, user_id: str, *, channel: str = TELEGRAM,
                        issued_by: str, ttl_seconds: int) -> tuple[str, datetime]:
    """Issue a fresh one-time code for this user+channel. Returns (display_code, expires_at).

    Any prior UNUSED code for the same user+channel is deleted first: exactly one code is live
    at a time, so an older screenshot cannot be redeemed.
    """
    if not user_id:
        raise LinkError("Your account is not resolved to a platform user yet — sign out and "
                        "back in, then try again.")
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LEN))
    expires_at = _now() + timedelta(seconds=max(60, int(ttl_seconds)))
    oid, uid = uuid.UUID(org_id), uuid.UUID(user_id)
    async with session_scope() as s:
        await s.execute(delete(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uid, ChannelLinkCode.channel == channel,
            ChannelLinkCode.used_at.is_(None)))
        s.add(ChannelLinkCode(org_id=oid, user_id=uid, channel=channel,
                              code_hash=_hash(raw), expires_at=expires_at))
        await _audit(s, org_id=oid, actor=issued_by, action="channel.link_code_issued",
                     target=channel,
                     detail={"channel": channel, "user_id": user_id,
                             "expires_at": expires_at.isoformat()})
    log.info("gateway.link_code_issued", channel=channel, user=issued_by)
    return _format(raw), expires_at


# ── redeeming ────────────────────────────────────────────────────────────────────────────────


async def consume_code(*, channel: str, code: str, channel_user_id: str, channel_chat_id: str,
                       channel_username: str | None) -> BoundIdentity:
    """Redeem a code and create the binding, or raise `LinkError` with a channel-safe message.

    Single-use is enforced by stamping `used_at` in the SAME transaction that writes the
    binding: a second redemption of the same code finds `used_at` set and is refused.
    """
    normalized = _normalize(code)
    if len(normalized) != _CODE_LEN:
        raise LinkError("That doesn't look like a link code. Generate one in AegisOps → "
                        "Settings → Connected accounts, then send `/link ABCD-EFGH`.")
    async with session_scope() as s:
        row = (await s.execute(select(ChannelLinkCode).where(
            ChannelLinkCode.channel == channel,
            ChannelLinkCode.code_hash == _hash(normalized)))).scalar_one_or_none()
        if row is None:
            raise LinkError("That link code isn't valid. Generate a new one in AegisOps → "
                            "Settings → Connected accounts.")
        if row.used_at is not None:
            raise LinkError("That link code has already been used. Codes are single-use — "
                            "generate a new one in AegisOps → Settings → Connected accounts.")
        if row.expires_at <= _now():
            raise LinkError("That link code has expired. Generate a new one in AegisOps → "
                            "Settings → Connected accounts.")

        # Is this chat account already bound — to this user (idempotent) or someone else (refuse)?
        existing_account = (await s.execute(select(ChannelIdentity).where(
            ChannelIdentity.channel == channel,
            ChannelIdentity.channel_user_id == str(channel_user_id)))).scalar_one_or_none()
        if existing_account is not None and existing_account.user_id != row.user_id:
            raise LinkError("This chat account is already linked to a different AegisOps user. "
                            "Unlink it there first (Settings → Connected accounts).")

        # Does this platform user already hold a DIFFERENT account on this channel? Re-linking
        # moves the binding to the new account — the code proves they authorized it.
        existing_user = (await s.execute(select(ChannelIdentity).where(
            ChannelIdentity.channel == channel,
            ChannelIdentity.user_id == row.user_id))).scalar_one_or_none()

        user = await s.get(User, row.user_id)
        if user is None:
            raise LinkError("The AegisOps account that issued this code no longer exists.")

        identity = existing_user or existing_account
        if identity is None:
            identity = ChannelIdentity(org_id=row.org_id, user_id=row.user_id, channel=channel,
                                       channel_user_id=str(channel_user_id),
                                       channel_chat_id=str(channel_chat_id),
                                       channel_username=channel_username)
            s.add(identity)
        else:
            identity.org_id = row.org_id
            identity.user_id = row.user_id
            identity.channel_user_id = str(channel_user_id)
            identity.channel_chat_id = str(channel_chat_id)
            identity.channel_username = channel_username
            identity.active_session_id = None  # a re-link starts a clean conversation
        identity.linked_by = user.username
        identity.linked_at = _now()
        identity.last_seen_at = _now()

        row.used_at = _now()
        row.used_by_channel_user_id = str(channel_user_id)

        await _audit(s, org_id=row.org_id, actor=user.username, action="channel.linked",
                     target=f"{channel}:{channel_user_id}",
                     detail={"channel": channel, "channel_username": channel_username,
                             "user_id": str(row.user_id), "relinked": identity is existing_user
                             and existing_user is not None})
        await s.flush()
        bound = _to_bound(identity, user)
    log.info("gateway.linked", channel=channel, user=bound.username)
    return bound


# ── resolving / unlinking ────────────────────────────────────────────────────────────────────


def _to_bound(identity: ChannelIdentity, user: User) -> BoundIdentity:
    return BoundIdentity(
        identity_id=str(identity.id),
        org_id=str(identity.org_id),
        user_id=str(identity.user_id),
        channel=identity.channel,
        channel_user_id=identity.channel_user_id,
        channel_chat_id=identity.channel_chat_id,
        username=user.username,
        email=user.email,
        name=user.name,
        roles=list(user.roles or []),
        active_session_id=str(identity.active_session_id) if identity.active_session_id else None,
    )


async def resolve(channel: str, channel_user_id: str) -> BoundIdentity | None:
    """The bound platform user for a channel account, or None when unbound.

    Called on EVERY inbound message and EVERY approval callback — capabilities are never
    cached in the chat, so Unlink (or a role change picked up at the next web login) takes
    effect on the very next interaction.
    """
    try:
        async with session_scope() as s:
            identity = (await s.execute(select(ChannelIdentity).where(
                ChannelIdentity.channel == channel,
                ChannelIdentity.channel_user_id == str(channel_user_id)))).scalar_one_or_none()
            if identity is None:
                return None
            user = await s.get(User, identity.user_id)
            if user is None:
                return None
            identity.last_seen_at = _now()
            return _to_bound(identity, user)
    except Exception as exc:  # noqa: BLE001 — an unreachable store must NOT authenticate anyone
        log.warning("gateway.resolve_failed", channel=channel, error=str(exc))
        return None


async def unlink(org_id: str, user_id: str, *, channel: str = TELEGRAM, actor: str) -> bool:
    """Cut the binding. Returns False when there was nothing linked. Audited either way."""
    oid, uid = uuid.UUID(org_id), uuid.UUID(user_id)
    async with session_scope() as s:
        identity = (await s.execute(select(ChannelIdentity).where(
            ChannelIdentity.channel == channel, ChannelIdentity.user_id == uid,
            ChannelIdentity.org_id == oid))).scalar_one_or_none()
        if identity is None:
            return False
        target = f"{channel}:{identity.channel_user_id}"
        await s.delete(identity)
        # Kill any live code too, so unlink really means "no path back in without a new code".
        await s.execute(delete(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uid, ChannelLinkCode.channel == channel,
            ChannelLinkCode.used_at.is_(None)))
        await _audit(s, org_id=oid, actor=actor, action="channel.unlinked", target=target,
                     detail={"channel": channel, "user_id": user_id})
    log.info("gateway.unlinked", channel=channel, actor=actor)
    return True


async def status(org_id: str, user_id: str, *, channel: str = TELEGRAM) -> dict:
    """The Settings panel's view: linked?, which account, when, and any live code's expiry."""
    if not user_id:
        return {"channel": channel, "linked": False, "code_pending": False}
    oid, uid = uuid.UUID(org_id), uuid.UUID(user_id)
    async with session_scope() as s:
        identity = (await s.execute(select(ChannelIdentity).where(
            ChannelIdentity.channel == channel, ChannelIdentity.user_id == uid,
            ChannelIdentity.org_id == oid))).scalar_one_or_none()
        pending = (await s.execute(select(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uid, ChannelLinkCode.channel == channel,
            ChannelLinkCode.used_at.is_(None),
            ChannelLinkCode.expires_at > _now()))).scalar_one_or_none()
        return {
            "channel": channel,
            "linked": identity is not None,
            "account": (identity.channel_username or identity.channel_user_id) if identity else None,
            "linked_at": identity.linked_at.isoformat() if identity else None,
            "linked_by": identity.linked_by if identity else None,
            # The code itself is never returned again — only its expiry, so the UI can keep
            # counting down after a refresh without the plaintext being re-servable.
            "code_pending": pending is not None,
            "code_expires_at": pending.expires_at.isoformat() if pending else None,
        }


async def set_active_session(identity_id: str, session_id: str | None) -> None:
    """Bind (or clear) the channel's current conversation — "one chat = one session"."""
    try:
        async with session_scope() as s:
            identity = await s.get(ChannelIdentity, uuid.UUID(identity_id))
            if identity is not None:
                identity.active_session_id = uuid.UUID(session_id) if session_id else None
    except Exception as exc:  # noqa: BLE001 — losing the pointer costs a new thread, not a run
        log.warning("gateway.set_active_session_failed", error=str(exc))


async def notifiable_approvers(org_id: str, *, channel: str = TELEGRAM,
                               exclude_user_id: str | None = None) -> list[BoundIdentity]:
    """Linked identities in this org whose bound user can approve — the push list for an
    approval card. Org-scoped (S0), and `exclude_user_id` drops the initiator when four-eyes
    means they cannot approve their own change anyway."""
    out: list[BoundIdentity] = []
    try:
        async with session_scope() as s:
            rows = (await s.execute(
                select(ChannelIdentity, User)
                .join(User, User.id == ChannelIdentity.user_id)
                .where(ChannelIdentity.channel == channel,
                       ChannelIdentity.org_id == uuid.UUID(org_id)))).all()
            for identity, user in rows:
                if exclude_user_id and str(identity.user_id) == str(exclude_user_id):
                    continue
                if not rbac.can_approve(list(user.roles or [])):
                    continue
                out.append(_to_bound(identity, user))
    except Exception as exc:  # noqa: BLE001 — a push-list failure must never fail a run
        log.warning("gateway.notifiable_approvers_failed", error=str(exc))
    return out


async def org_slug(org_id: str) -> str:
    async with session_scope() as s:
        org = await s.get(Organization, uuid.UUID(org_id))
        return org.slug if org else ""
