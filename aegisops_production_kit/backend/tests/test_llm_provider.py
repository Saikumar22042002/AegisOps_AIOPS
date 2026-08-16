"""U3 — model validation seam + honest model catalog (P1: multi-provider substrate).

The model an operator picks is validated against the models the backend actually serves;
an unknown model fails loudly rather than being silently ignored; the per-run model pin
lives in a contextvar (never shared state) so concurrent runs stay isolated. P1 note:
the catalog is now MULTI-provider by design — enabled/selectable is governed by which
providers carry credentials, never by hardcoding one vendor.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.integrations.gemini import get_run_model, set_run_model
from app.llm import catalog as llm_catalog
from app.llm.errors import ModelError
from app.main import app
from app.schemas.auth import User
from app.security import rbac
from app.security.deps import get_current_user, require_auth
from app.settings import Settings, get_settings


def _user(roles: list[str]) -> User:
    return User(sub="t", username="tester", email="t@example.com", name="Tester", roles=roles,
                display_roles=rbac.display_roles(roles), can_approve=rbac.can_approve(roles),
                can_initiate=rbac.can_initiate(roles), can_execute=rbac.can_execute(roles),
                org="acme")


def test_default_model_when_none_requested():
    """Admission with no model resolves the catalog's general-purpose default — the SAME
    model the retired provider seam defaulted to (behavioral parity with U3)."""
    assert llm_catalog.load().purposes["general"].model == get_settings().gemini_model


def test_known_model_resolves_and_unknown_raises_clear_error():
    cat = llm_catalog.load()
    assert cat.model("gemini-2.5-flash").id == "gemini-2.5-flash"
    with pytest.raises(ModelError) as exc:
        cat.model("gpt-o9-ultra")
    msg = str(exc.value)
    assert exc.value.kind == "invalid_request"
    assert "gpt-o9-ultra" in msg and "AegisOps serves" in msg  # names what it will accept


def test_catalog_is_multi_provider_and_marks_one_default():
    """P1 inversion of the old all-gemini pin: the catalog declares several wire families;
    credentials decide what is ENABLED, the catalog decides what EXISTS."""
    cat = llm_catalog.load()
    assert {"google", "anthropic", "openai_compat"} <= {m.provider
                                                        for m in cat.models.values()}
    chat_models = [m for m in cat.models.values() if "embeddings" not in m.capabilities]
    default = cat.purposes["general"].model
    assert sum(1 for m in chat_models if m.id == default) == 1
    # Credentials gate selectability per provider — never a hardcoded vendor branch.
    ids = {m.id for m in cat.configured_models(Settings(gemini_api_key="k"))}
    assert "gemini-3.5-flash" in ids and "claude-sonnet-5" not in ids
    both = Settings(gemini_api_key="k", anthropic_api_key="a")
    assert "claude-sonnet-5" in {m.id for m in cat.configured_models(both)}


def test_run_model_contextvar_is_per_task_isolated():
    """Two concurrent runs binding different models must not clobber each other — the whole
    reason the choice lives in a contextvar and not on shared client state."""
    async def _bind_and_read(model, hold):
        set_run_model(model)
        await asyncio.sleep(hold)  # yield so the other task interleaves
        return get_run_model()

    async def _main():
        return await asyncio.gather(
            _bind_and_read("gemini-2.5-flash", 0.02),
            _bind_and_read("gemini-flash-latest", 0.0),
        )

    a, b = asyncio.run(_main())
    assert a == "gemini-2.5-flash" and b == "gemini-flash-latest"


def test_models_endpoint_returns_the_real_catalog():
    """GET /models exposes exactly what admission validates against — the menu's source.
    Shape frozen (FE-05): {id, provider, enabled, default}; embeddings models excluded."""
    app.dependency_overrides[require_auth] = lambda: _user(["developer"])
    try:
        with TestClient(app) as c:
            r = c.get("/models")
        assert r.status_code == 200
        models = r.json()["models"]
    finally:
        app.dependency_overrides.pop(require_auth, None)
    assert models and all(set(m) >= {"id", "provider", "enabled", "default"} for m in models)
    assert sum(1 for m in models if m["default"]) == 1
    ids = {m["id"] for m in models}
    assert "gemini-embedding-001" not in ids
    cat = llm_catalog.load()
    assert ids == {m.id for m in cat.models.values() if "embeddings" not in m.capabilities}


def test_chat_rejects_unknown_model_with_400_before_touching_db():
    """U3: an unknown model is a clear 400 at the boundary — never silently swapped for the
    default. This runs before any datastore access, so it is safe in the unit tier."""
    app.dependency_overrides[get_current_user] = lambda: _user(["developer"])
    try:
        with TestClient(app) as c:
            r = c.post("/chat", json={"message": "hi", "model": "gpt-4o"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "gpt-4o" in detail and "AegisOps serves" in detail
    finally:
        app.dependency_overrides.pop(get_current_user, None)
