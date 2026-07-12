"""U7 — retry-with-fix + undo last apply.

A classified provider failure can carry a one-click retry suggestion (the user's own message
with only the fix applied — a genuine new turn through the whole gated flow), and "undo that"
deterministically targets the LAST resource this conversation applied via the normal
approval-gated destroy. Never a silent mutation in either direction.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.agents import intent_guard, inventory
from app.agents.events import Emitter, RunChannel
from app.agents.provider_errors import ProviderFailure, suggest_retry
from app.db.models import Resource
from app.db.session import session_scope


# ── suggest_retry ──────────────────────────────────────────────────────────────────────────

def _bad_region():
    return ProviderFailure(kind="bad_region", title="t", cause="c", next_step="n")


def test_bad_region_swaps_the_region_in_place():
    r = suggest_retry(_bad_region(), "create a vm named web, region=xx-bad-9",
                      cloud="aws", current_region="xx-bad-9")
    assert r["retry_message"] == "create a vm named web, region=us-east-1"
    assert r["to"] == "us-east-1" and r["from"] == "xx-bad-9"
    assert "us-east-1" in r["label"]


def test_bad_region_appends_when_none_named_and_respects_cloud():
    r = suggest_retry(_bad_region(), "create a storage bucket", cloud="gcp")
    assert r["retry_message"] == "create a storage bucket, region=us-central1"
    r2 = suggest_retry(_bad_region(), "a storage account, location: badloc", cloud="azure")
    assert r2["retry_message"] == "a storage account, location: eastus"


def test_alternate_never_repeats_the_failed_region():
    r = suggest_retry(_bad_region(), "vm please, region=us-east-1",
                      cloud="aws", current_region="us-east-1")
    assert r["to"] != "us-east-1" and r["to"] == "us-west-2"


def test_credentials_expired_offers_same_message_retry():
    f = ProviderFailure(kind="credentials_expired", title="t", cause="c", next_step="n")
    r = suggest_retry(f, "create an s3 bucket b1")
    assert r["retry_message"] == "create an s3 bucket b1"
    assert "credentials" in r["label"].lower()


def test_unclassified_or_unfixable_failures_suggest_nothing():
    assert suggest_retry(None, "anything") is None
    f = ProviderFailure(kind="api_disabled", title="t", cause="c", next_step="n")
    assert suggest_retry(f, "anything") is None  # no honest one-click fix exists


async def test_error_event_carries_the_retry_payload():
    ch = RunChannel("u7-run")
    em = Emitter(ch)
    retry = {"kind": "bad_region", "label": "Retry with region us-west-2",
             "retry_message": "vm, region=us-west-2"}
    await em.error("terraform plan failed: bad region", code="terraform_error",
                   retriable=True, retry=retry)
    await em.error("plain failure", code="x")  # without a retry → no key at all
    ev1, ev2 = ch.history[0]["data"], ch.history[1]["data"]
    assert ev1["retry"] == retry
    assert "retry" not in ev2


# ── undo detection ─────────────────────────────────────────────────────────────────────────

def test_is_undo_matches_natural_shapes():
    for msg in ("undo that", "Undo it", "please undo the last apply", "revert that change",
                "undo this deployment", "undo"):
        assert intent_guard.is_undo(msg), msg


def test_is_undo_ignores_unanchored_sentences():
    for msg in ("undo is a great feature generally", "revert engineering blog post",
                "create a vm named undo-proof"):
        assert not intent_guard.is_undo(msg), msg


def test_undo_counts_as_explicitly_destructive():
    assert intent_guard.explicitly_destructive("undo that")
    assert intent_guard.explicitly_destructive("revert the last apply")


# ── last-applied resolution (session-scoped) ───────────────────────────────────────────────

async def test_last_applied_is_session_scoped_and_newest(live_db, throwaway_org):
    org = throwaway_org
    sess_a, sess_b = str(uuid.uuid4()), str(uuid.uuid4())
    from app.db.models import Session as DbSession

    async with session_scope() as s:
        for sid in (sess_a, sess_b):
            s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="t"))
    # Each apply commits in its OWN transaction in production, so created_at differs per row —
    # mirror that here (Postgres now() is transaction-stable, so one txn would tie the stamps).
    for name, sid in (("older-vm", sess_a), ("newer-vm", sess_a), ("other-sess-vm", sess_b)):
        async with session_scope() as s:
            s.add(Resource(org_id=uuid.UUID(org), session_id=uuid.UUID(sid), name=name,
                           cloud="aws", resource_type="ec2", workspace="aws-ec2",
                           status="active"))
    try:
        last = await inventory.last_applied(org, sess_a)
        assert last is not None and last["name"] == "newer-vm"   # newest, THIS session only
        assert await inventory.last_applied(org, str(uuid.uuid4())) is None  # empty session
        assert await inventory.last_applied(org, None) is None
    finally:
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.org_id == uuid.UUID(org)))


async def test_last_applied_skips_destroyed_rows(live_db, throwaway_org):
    org = throwaway_org
    sid = str(uuid.uuid4())
    from app.db.models import Session as DbSession

    async with session_scope() as s:
        s.add(DbSession(id=uuid.UUID(sid), org_id=uuid.UUID(org), title="t"))
        await s.flush()
        s.add(Resource(org_id=uuid.UUID(org), session_id=uuid.UUID(sid), name="already-gone",
                       cloud="aws", resource_type="ec2", workspace="aws-ec2",
                       status="destroyed"))
    try:
        assert await inventory.last_applied(org, sid) is None  # nothing ACTIVE to undo
    finally:
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
            await s.execute(delete(DbSession).where(DbSession.org_id == uuid.UUID(org)))


# ── router fast-path ───────────────────────────────────────────────────────────────────────

async def test_router_routes_undo_deterministically(monkeypatch, live_redis):
    """"undo that" routes to cloudops destroy of __last_applied__ with NO LLM involved."""
    from app.agents import router as router_mod

    class _Emitter:
        async def step(self, *a, **k): pass

    monkeypatch.setattr(router_mod, "emitter_of", lambda cfg: _Emitter())

    async def _no_pending(session_id):
        return None
    monkeypatch.setattr(router_mod.params, "load_pending", _no_pending)

    out = await router_mod.router({"message": "undo that", "session_id": str(uuid.uuid4()),
                                   "org_id": str(uuid.uuid4()), "run_id": "r1", "user": {}}, {})
    assert out["domain"] == "cloudops" and out["action"] == "destroy"
    assert out["target"] == "__last_applied__"
    assert out["intent"] == "undo_last_apply" and out["intent_confidence"] == 1.0
