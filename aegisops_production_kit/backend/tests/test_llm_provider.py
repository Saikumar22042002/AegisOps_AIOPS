"""U3 — real LLM provider seam + honest model catalog.

The model an operator picks is validated against the models the backend actually serves; an
unknown model fails loudly rather than being silently ignored, and the per-run model is bound
to the calling context (never mutated onto the shared client) so concurrent runs stay isolated.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.integrations import gemini as gemini_mod
from app.integrations.gemini import get_run_model, set_run_model
from app.integrations.llm import UnknownModelError, available_models, get_provider
from app.main import app
from app.schemas.auth import User
from app.security import rbac
from app.security.deps import get_current_user, require_auth
from app.settings import get_settings


def _user(roles: list[str]) -> User:
    return User(sub="t", username="tester", email="t@example.com", name="Tester", roles=roles,
                display_roles=rbac.display_roles(roles), can_approve=rbac.can_approve(roles),
                can_initiate=rbac.can_initiate(roles), can_execute=rbac.can_execute(roles), org="acme")


def test_default_model_when_none_requested():
    settings = get_settings()
    provider, model = get_provider(settings, None)
    assert provider.name == "google-gemini"
    assert model == settings.gemini_model  # the configured default, not a random pick


def test_blank_model_is_treated_as_default():
    settings = get_settings()
    _p, model = get_provider(settings, "   ")
    assert model == settings.gemini_model


def test_known_model_resolves_to_that_model():
    settings = get_settings()
    provider, model = get_provider(settings, "gemini-2.5-flash")
    assert provider.serves("gemini-2.5-flash") and model == "gemini-2.5-flash"


def test_unknown_model_raises_clear_error():
    settings = get_settings()
    with pytest.raises(UnknownModelError) as exc:
        get_provider(settings, "gpt-4o")
    msg = str(exc.value)
    assert "gpt-4o" in msg and "AegisOps serves" in msg  # names what it will accept


def test_catalog_is_all_gemini_and_marks_a_default():
    settings = get_settings()
    catalog = available_models(settings)
    assert catalog, "catalog must not be empty"
    assert all(m["provider"] == "google-gemini" for m in catalog)
    assert sum(1 for m in catalog if m["default"]) == 1  # exactly one default
    assert settings.gemini_model in {m["id"] for m in catalog}
    # No phantom vendors advertised.
    ids = " ".join(m["id"] for m in catalog).lower()
    assert "claude" not in ids and "gpt" not in ids and "llama" not in ids


def test_run_model_contextvar_is_per_task_isolated():
    """Two concurrent runs binding different models must not clobber each other — the whole
    reason the choice lives in a contextvar and not on the shared GeminiLLM singleton."""
    async def _bind_and_read(model, hold):
        set_run_model(model)
        await asyncio.sleep(hold)  # yield so the other task interleaves
        return get_run_model()

    async def _main():
        a, b = await asyncio.gather(
            _bind_and_read("gemini-2.5-flash", 0.02),
            _bind_and_read("gemini-flash-latest", 0.0),
        )
        return a, b

    a, b = asyncio.run(_main())
    assert a == "gemini-2.5-flash" and b == "gemini-flash-latest"


def test_effective_model_prefers_explicit_then_run_then_default(monkeypatch):
    settings = get_settings()
    llm = gemini_mod.get_gemini(settings)
    monkeypatch.setattr(llm, "model", "default-model")
    # Bind a run model in THIS context so the contextvar branch is exercised deterministically.
    token = gemini_mod._run_model.set("run-model")
    try:
        assert llm._effective_model("explicit") == "explicit"   # explicit wins
        assert llm._effective_model(None) == "run-model"        # then the per-run choice
    finally:
        gemini_mod._run_model.reset(token)
    assert llm._effective_model(None) == "default-model"        # else the resolved default


def test_models_endpoint_returns_the_real_catalog():
    """GET /models exposes exactly what get_provider validates against — the menu's source."""
    app.dependency_overrides[require_auth] = lambda: _user(["developer"])
    try:
        with TestClient(app) as c:
            r = c.get("/models")
        assert r.status_code == 200
        models = r.json()["models"]
        assert models and all(m["provider"] == "google-gemini" for m in models)
        assert get_settings().gemini_model in {m["id"] for m in models}
    finally:
        app.dependency_overrides.pop(require_auth, None)


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
