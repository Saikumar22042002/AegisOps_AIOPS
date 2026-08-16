"""P1.7 — model bindings: catalog-validated writes, eval-gated routing, RBAC, audit.

DB-free pins run everywhere (validation happens BEFORE any session is opened; RBAC
happens at the API layer); the live round-trip (set → resolve → clear + audit row)
is integration-tier and runs against the real dev/container PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest

from app.llm import bindings
from app.llm.errors import ModelError
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── catalog validation happens before any DB touch ──────────────────────────────────────────

async def test_set_binding_refuses_unknown_model_without_db():
    with pytest.raises(ModelError) as e:
        await bindings.set_binding(str(uuid.uuid4()), "knowledge", "gpt-o9-ultra",
                                   actor="t", reason=None,
                                   settings=Settings(gemini_api_key="k"))
    assert e.value.kind == "invalid_request"


async def test_set_binding_refuses_capability_mismatch_without_db():
    with pytest.raises(ModelError, match="lacks"):
        await bindings.set_binding(str(uuid.uuid4()), "extract", "gemini-embedding-001",
                                   actor="t", reason=None,
                                   settings=Settings(gemini_api_key="k"))


async def test_set_binding_refuses_unconfigured_provider_without_db():
    """The §4 dead-end guard: binding a purpose to a keyless provider is refused."""
    with pytest.raises(ModelError, match="no credentials"):
        await bindings.set_binding(str(uuid.uuid4()), "knowledge", "claude-sonnet-5",
                                   actor="t", reason=None,
                                   settings=Settings(gemini_api_key="k",
                                                     anthropic_api_key=""))


async def test_set_binding_refuses_unknown_purpose_without_db():
    with pytest.raises(ModelError, match="unknown purpose"):
        await bindings.set_binding(str(uuid.uuid4()), "vibes", "gemini-3.5-flash",
                                   actor="t", reason=None,
                                   settings=Settings(gemini_api_key="k"))


# ── API RBAC (no DB: the 403 fires before any session) ──────────────────────────────────────

def test_binding_writes_are_admin_gated():
    from fastapi import HTTPException

    from app.api.integrations import _require_binding_admin
    from app.schemas.auth import User

    def user(*roles):
        return User(sub="u", username="u", email="u@x", name="U", roles=list(roles),
                    display_roles=[], can_approve=False, can_initiate=True,
                    can_execute=False, org="o", org_id=str(uuid.uuid4()), user_id="u1")

    for ok_role in ("org-admin", "platform-admin"):
        _require_binding_admin(user(ok_role))       # must not raise
    for bad in (user("cloud-architect"), user("devops-engineer"), user()):
        with pytest.raises(HTTPException) as e:
            _require_binding_admin(bad)
        assert e.value.status_code == 403


# ── live round-trip (integration tier: real PostgreSQL) ─────────────────────────────────────

@pytest.mark.usefixtures("live_db")
async def test_binding_round_trip_routes_and_audits(throwaway_org):
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.session import session_scope
    from app.llm import router
    from app.llm.types import GOVERNED_PURPOSES

    s = Settings(gemini_api_key="k")
    org_id = throwaway_org
    out = await bindings.set_binding(org_id, "knowledge", "gemini-2.5-flash",
                                     actor="itest", reason="pin for test", settings=s)
    assert out["eval_state"] == "pending"           # eval-gated promotion starts pending
    bindings._invalidate()
    assert await bindings.resolve(org_id, "knowledge") == "gemini-2.5-flash"

    # The router consumes it through the registered hook (pin → BINDING → default).
    bindings.register()
    try:
        plan = await router.resolve(s, "knowledge", org_id=org_id)
        assert plan.model == "gemini-2.5-flash" and plan.pinned_by == "binding"
        assert "knowledge" not in GOVERNED_PURPOSES
    finally:
        router.set_binding_resolver(None)

    # Every write lands an audit row (bindings move model traffic — a governed act).
    async with session_scope() as sess:
        rows = (await sess.execute(select(AuditLog).where(
            AuditLog.org_id == uuid.UUID(org_id),
            AuditLog.action == "model_binding.set"))).scalars().all()
    assert rows and rows[-1].target == "knowledge→gemini-2.5-flash"

    await bindings.clear_binding(org_id, "knowledge", actor="itest", settings=s)
    bindings._invalidate()
    assert await bindings.resolve(org_id, "knowledge") is None
