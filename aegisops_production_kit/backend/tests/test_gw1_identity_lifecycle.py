"""GW-1 (integration tier) — the link-code lifecycle against the real Postgres schema.

Skips cleanly without datastores (see conftest `live_db`), runs for real under `make test`.

What these lock down, end to end on real rows:

* a code is **single-use** — the second redemption is refused, not silently re-bound;
* a code **expires** — a past `expires_at` is refused;
* **only the hash is stored** — the plaintext code never appears in the table;
* issuing a code **invalidates** the issuer's previous unused code (a stale screenshot is dead);
* **audit rows** are written on issue, link and unlink;
* `resolve()` returns the bound platform user with their real roles, and `unlink()` makes it
  unresolvable again AND kills any live code;
* a channel account already bound to a DIFFERENT user cannot be stolen with a fresh code.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.models import AuditLog, ChannelIdentity, ChannelLinkCode, User
from app.db.session import session_scope
from app.gateways import identity

TTL = 600


@pytest.fixture
async def member(throwaway_org: str):
    """A real `users` row in a throwaway org, with approver roles. Cleaned up after."""
    async with session_scope() as s:
        row = User(org_id=uuid.UUID(throwaway_org), username="gw1-tester",
                   email="gw1@example.com", name="GW1 Tester",
                   roles=["cloud-architect"])
        s.add(row)
        await s.flush()
        uid = str(row.id)
    yield {"org_id": throwaway_org, "user_id": uid, "username": "gw1-tester"}
    async with session_scope() as s:
        await s.execute(delete(ChannelIdentity).where(ChannelIdentity.user_id == uuid.UUID(uid)))
        await s.execute(delete(ChannelLinkCode).where(ChannelLinkCode.user_id == uuid.UUID(uid)))
        await s.execute(delete(User).where(User.id == uuid.UUID(uid)))


async def _audit_actions(org_id: str) -> list[str]:
    async with session_scope() as s:
        rows = (await s.execute(select(AuditLog.action).where(
            AuditLog.org_id == uuid.UUID(org_id)))).scalars().all()
    return list(rows)


async def test_full_link_lifecycle_is_single_use_and_audited(member):
    code, expires_at = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)

    assert "-" in code and len(code.replace("-", "")) == 8
    assert expires_at > datetime.now(timezone.utc)

    # Only the HASH is stored — a DB read cannot harvest a live code.
    async with session_scope() as s:
        stored = (await s.execute(select(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uuid.UUID(member["user_id"])))).scalars().all()
    assert len(stored) == 1
    assert stored[0].code_hash != code.replace("-", "")
    assert len(stored[0].code_hash) == 64
    assert code.replace("-", "") not in stored[0].code_hash

    bound = await identity.consume_code(channel="telegram", code=code, channel_user_id="777001",
                                        channel_chat_id="777001", channel_username="sai_tg")
    assert bound.username == "gw1-tester"
    assert bound.org_id == member["org_id"]
    assert bound.roles == ["cloud-architect"]
    assert bound.auth_user().can_approve is True

    # resolve() finds it on the next message.
    again = await identity.resolve("telegram", "777001")
    assert again is not None and again.user_id == member["user_id"]

    # SINGLE-USE: the same code cannot be redeemed twice.
    with pytest.raises(identity.LinkError, match="already been used"):
        await identity.consume_code(channel="telegram", code=code, channel_user_id="777002",
                                    channel_chat_id="777002", channel_username="thief")

    actions = await _audit_actions(member["org_id"])
    assert "channel.link_code_issued" in actions
    assert "channel.linked" in actions

    # Unlink removes the binding and is audited.
    assert await identity.unlink(member["org_id"], member["user_id"], actor=member["username"])
    assert await identity.resolve("telegram", "777001") is None
    assert "channel.unlinked" in await _audit_actions(member["org_id"])

    # Idempotent: a second unlink is a clean False, not an error.
    assert await identity.unlink(member["org_id"], member["user_id"],
                                 actor=member["username"]) is False


async def test_expired_code_is_refused(member):
    code, _ = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)
    async with session_scope() as s:
        row = (await s.execute(select(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uuid.UUID(member["user_id"])))).scalar_one()
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(identity.LinkError, match="expired"):
        await identity.consume_code(channel="telegram", code=code, channel_user_id="777010",
                                    channel_chat_id="777010", channel_username="sai_tg")
    assert await identity.resolve("telegram", "777010") is None


async def test_reissuing_invalidates_the_previous_code(member):
    first, _ = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)
    second, _ = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)
    assert first != second

    with pytest.raises(identity.LinkError, match="isn't valid"):
        await identity.consume_code(channel="telegram", code=first, channel_user_id="777020",
                                    channel_chat_id="777020", channel_username="sai_tg")
    bound = await identity.consume_code(channel="telegram", code=second,
                                        channel_user_id="777020", channel_chat_id="777020",
                                        channel_username="sai_tg")
    assert bound.user_id == member["user_id"]


async def test_garbage_and_wrong_codes_are_refused(member):
    for bad in ("", "nope", "AAAA-BBBB", "1234567"):
        with pytest.raises(identity.LinkError):
            await identity.consume_code(channel="telegram", code=bad, channel_user_id="777030",
                                        channel_chat_id="777030", channel_username="x")
    assert await identity.resolve("telegram", "777030") is None


async def test_code_is_accepted_in_any_typed_form(member):
    code, _ = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)
    # Shown as ABCD-EFGH; retyped lowercase, spaced, ungrouped — all the same code.
    typed = code.replace("-", " ").lower()
    bound = await identity.consume_code(channel="telegram", code=typed,
                                        channel_user_id="777040", channel_chat_id="777040",
                                        channel_username="sai_tg")
    assert bound.user_id == member["user_id"]


async def test_a_bound_account_cannot_be_stolen_by_another_user(throwaway_org, member):
    """Two platform users, one Telegram account: the second must be refused, not re-bound."""
    async with session_scope() as s:
        other = User(org_id=uuid.UUID(throwaway_org), username="gw1-other",
                     email="other@example.com", roles=["devops-engineer"])
        s.add(other)
        await s.flush()
        other_id = str(other.id)

    code_a, _ = await identity.generate_code(member["org_id"], member["user_id"],
                                             issued_by=member["username"], ttl_seconds=TTL)
    await identity.consume_code(channel="telegram", code=code_a, channel_user_id="777050",
                                channel_chat_id="777050", channel_username="sai_tg")

    code_b, _ = await identity.generate_code(throwaway_org, other_id, issued_by="gw1-other",
                                             ttl_seconds=TTL)
    with pytest.raises(identity.LinkError, match="already linked to a different"):
        await identity.consume_code(channel="telegram", code=code_b, channel_user_id="777050",
                                    channel_chat_id="777050", channel_username="sai_tg")

    # The original binding is intact.
    still = await identity.resolve("telegram", "777050")
    assert still is not None and still.user_id == member["user_id"]

    async with session_scope() as s:
        await s.execute(delete(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uuid.UUID(other_id)))
        await s.execute(delete(User).where(User.id == uuid.UUID(other_id)))


async def test_status_reports_link_and_pending_code_without_the_code(member):
    st = await identity.status(member["org_id"], member["user_id"])
    assert st["linked"] is False and st["code_pending"] is False

    code, expires_at = await identity.generate_code(
        member["org_id"], member["user_id"], issued_by=member["username"], ttl_seconds=TTL)
    st = await identity.status(member["org_id"], member["user_id"])
    assert st["code_pending"] is True and st["code_expires_at"] is not None
    # The plaintext is NEVER re-servable — only its expiry.
    assert code not in str(st)

    await identity.consume_code(channel="telegram", code=code, channel_user_id="777060",
                                channel_chat_id="777060", channel_username="sai_tg")
    st = await identity.status(member["org_id"], member["user_id"])
    assert st["linked"] is True and st["account"] == "sai_tg"
    assert st["code_pending"] is False       # consumed


async def test_unlink_kills_a_live_code_too(member):
    code_a, _ = await identity.generate_code(member["org_id"], member["user_id"],
                                             issued_by=member["username"], ttl_seconds=TTL)
    await identity.consume_code(channel="telegram", code=code_a, channel_user_id="777070",
                                channel_chat_id="777070", channel_username="sai_tg")
    # A second code is issued and left unused, then the user unlinks.
    code_b, _ = await identity.generate_code(member["org_id"], member["user_id"],
                                             issued_by=member["username"], ttl_seconds=TTL)
    await identity.unlink(member["org_id"], member["user_id"], actor=member["username"])

    with pytest.raises(identity.LinkError, match="isn't valid"):
        await identity.consume_code(channel="telegram", code=code_b, channel_user_id="777070",
                                    channel_chat_id="777070", channel_username="sai_tg")


async def test_notifiable_approvers_is_org_scoped_and_role_filtered(throwaway_org, member):
    """The approval push list: linked + can_approve + same org, minus the excluded initiator."""
    async with session_scope() as s:
        reader = User(org_id=uuid.UUID(throwaway_org), username="gw1-reader",
                      email="r@example.com", roles=["auditor"])
        s.add(reader)
        await s.flush()
        reader_id = str(reader.id)

    code_a, _ = await identity.generate_code(member["org_id"], member["user_id"],
                                             issued_by=member["username"], ttl_seconds=TTL)
    await identity.consume_code(channel="telegram", code=code_a, channel_user_id="777080",
                                channel_chat_id="777080", channel_username="approver_tg")
    code_r, _ = await identity.generate_code(throwaway_org, reader_id, issued_by="gw1-reader",
                                             ttl_seconds=TTL)
    await identity.consume_code(channel="telegram", code=code_r, channel_user_id="777081",
                                channel_chat_id="777081", channel_username="reader_tg")

    everyone = await identity.notifiable_approvers(throwaway_org)
    usernames = {b.username for b in everyone}
    assert "gw1-tester" in usernames          # cloud-architect → can approve
    assert "gw1-reader" not in usernames      # auditor → cannot approve, never pushed to

    excluded = await identity.notifiable_approvers(throwaway_org,
                                                   exclude_user_id=member["user_id"])
    assert {b.username for b in excluded} == set()

    # A different org sees none of them (S0).
    async with session_scope() as s:
        from app.db.models import Organization
        other_org = Organization(name="gw1 other", slug=f"gw1-other-{uuid.uuid4().hex[:8]}")
        s.add(other_org)
        await s.flush()
        other_org_id = str(other_org.id)
    assert await identity.notifiable_approvers(other_org_id) == []

    async with session_scope() as s:
        from app.db.models import Organization
        await s.execute(delete(ChannelIdentity).where(
            ChannelIdentity.user_id == uuid.UUID(reader_id)))
        await s.execute(delete(ChannelLinkCode).where(
            ChannelLinkCode.user_id == uuid.UUID(reader_id)))
        await s.execute(delete(User).where(User.id == uuid.UUID(reader_id)))
        await s.execute(delete(Organization).where(Organization.id == uuid.UUID(other_org_id)))
