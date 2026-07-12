"""M4 — per-user/org persistent memory: user-editable standing context that survives sessions,
threaded into build_context, and honored deterministically ("my usual region") without an LLM.

Integration (live Postgres).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.agents import memory, user_memory
from app.db.models import UserMemory, User
from app.db.session import session_scope


@pytest.fixture
async def mem_user(throwaway_org):
    """A user row in the throwaway org (user_memories.user_id is FK → users.id)."""
    async with session_scope() as s:
        u = User(org_id=uuid.UUID(throwaway_org), username=f"mem-{uuid.uuid4().hex[:8]}",
                 email=f"mem-{uuid.uuid4().hex[:6]}@t.io")
        s.add(u)
        await s.flush()
        uid = str(u.id)
    yield throwaway_org, uid
    async with session_scope() as s:
        await s.execute(delete(UserMemory).where(UserMemory.org_id == uuid.UUID(throwaway_org)))
        await s.execute(delete(User).where(User.id == uuid.UUID(uid)))


async def test_set_get_upsert_delete_roundtrip(live_db, mem_user):
    org, uid = mem_user
    await user_memory.set_memory(org, uid, "usual_region", "ap-south-1")
    assert await user_memory.lookup(org, uid, "usual_region") == "ap-south-1"
    await user_memory.set_memory(org, uid, "usual_region", "eu-west-1")   # upsert overwrites
    assert await user_memory.lookup(org, uid, "usual_region") == "eu-west-1"
    rows = await user_memory.list_memories(org, uid)
    assert [r["key"] for r in rows] == ["usual_region"]
    assert await user_memory.delete_memory(org, uid, "usual_region") is True
    assert await user_memory.lookup(org, uid, "usual_region") is None


async def test_org_wide_memory_visible_to_members_user_row_wins(live_db, mem_user):
    org, uid = mem_user
    await user_memory.set_memory(org, None, "usual_region", "us-east-2")       # org-wide
    assert await user_memory.lookup(org, uid, "usual_region") == "us-east-2"   # member sees it
    await user_memory.set_memory(org, uid, "usual_region", "ap-south-1")       # personal
    assert await user_memory.lookup(org, uid, "usual_region") == "ap-south-1"  # user row wins
    rows = await user_memory.list_memories(org, uid)
    assert {(r["key"], r["scope"]) for r in rows} == {("usual_region", "org"),
                                                      ("usual_region", "user")}


async def test_memory_is_org_scoped(live_db, mem_user):
    org, uid = mem_user
    await user_memory.set_memory(org, uid, "usual_region", "ap-south-1")
    other_org = str(uuid.uuid4())  # a different org's view — nothing leaks
    assert await user_memory.render_block(other_org, None) == ""


async def test_build_context_leads_with_standing_memory_in_a_new_session(live_db, mem_user):
    """Acceptance: 'my usual region' honored in a NEW session — the standing block is
    session-independent, so a brand-new session's very first context already carries it."""
    org, uid = mem_user
    await user_memory.set_memory(org, uid, "usual_region", "ap-south-1")
    fresh_session = str(uuid.uuid4())  # no messages at all
    ctx = await memory.build_context(fresh_session, purpose="general",
                                     current_message="create a vm",
                                     org_id=org, user_id=uid)
    assert ctx.startswith("Standing user memory")
    assert "usual_region: ap-south-1" in ctx


async def test_usual_region_is_honored_deterministically_without_llm(live_db, mem_user):
    """The no-LLM path: a request that says "my usual region" resolves the region from the
    standing memory in input extraction — an explicit region in the message still wins."""
    from app.agents.cloudops import _extract_inputs
    from app.agents.templates import by_key
    from app.settings import get_settings

    org, uid = mem_user
    await user_memory.set_memory(org, uid, "usual_region", "ap-south-1")
    settings = get_settings()

    t = by_key("aws.s3")
    inputs = await _extract_inputs(settings, t, "create an s3 bucket in my usual region, "
                                                "bucket_name=phx-logs",
                                   org_id=org, user_id=uid)
    assert inputs["region"] == "ap-south-1"
    assert inputs["bucket_name"] == "phx-logs"

    # Explicit region beats the memory.
    inputs2 = await _extract_inputs(settings, t, "bucket in my usual region, region=eu-west-1",
                                    org_id=org, user_id=uid)
    assert inputs2["region"] == "eu-west-1"

    # Azure templates map the memory onto `location`.
    az = by_key("azure.storage")
    inputs3 = await _extract_inputs(settings, az, "storage account in my usual region",
                                    org_id=org, user_id=uid)
    assert inputs3["location"] == "ap-south-1"


async def test_render_block_is_bounded_and_empty_when_unset(live_db, mem_user):
    org, uid = mem_user
    assert await user_memory.render_block(org, uid) == ""      # nothing set → no fake block
    await user_memory.set_memory(org, uid, "notes", "x" * 2000)
    block = await user_memory.render_block(org, uid)
    assert len(block) <= 600                                    # bounded prompt slice
