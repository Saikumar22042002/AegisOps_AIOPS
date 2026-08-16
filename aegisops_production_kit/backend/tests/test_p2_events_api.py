"""P2.5 — GET /runs/{id}/events contract: CoT-safe harness loop trail, org-scoped.

The endpoint must expose the OBSERVE→REASON→ACT trail (hypothesis one-liners, rationale
summaries, ok/failed observations, verification) and NEVER raw chain-of-thought. Org
predicate (S2) is inherited from the shared artifact loader. Live tier (real PostgreSQL).
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_loop_kinds_exclude_nothing_secret_and_endpoint_never_returns_cot():
    """Static guard: the endpoint's projection maps assistant_turn to hypothesis+rationale
    only — it has no code path that could emit a raw-CoT field."""
    import inspect

    from app.api import artifacts
    src = inspect.getsource(artifacts.run_events)
    # only the safe fields are read off assistant_turn payloads
    assert 'p.get("hypothesis")' in src and 'p.get("rationale")' in src
    assert "chain_of_thought" not in src and "raw_reasoning" not in src


@pytest.mark.usefixtures("live_db")
async def test_events_endpoint_returns_cot_safe_trail(throwaway_org):
    from httpx import ASGITransport, AsyncClient

    from app.db.models import Run
    from app.db.session import session_scope
    from app.harness import run_log
    from app.main import app
    from app.schemas.auth import User
    from app.security import rbac
    from app.security.deps import get_current_user

    org = throwaway_org
    async with session_scope() as s:
        run = Run(org_id=uuid.UUID(org), status="completed", mode="plan",
                  domain="sre", intent="triage")
        s.add(run)
        await s.flush()
        run_id = str(run.id)

    # A representative harness trail: a failed read, a changed hypothesis, recovery, verify.
    await run_log.append(run_id, "iteration_started", {"n": 1}, org_id=org)
    await run_log.append(run_id, "assistant_turn",
                         {"hypothesis": "metric is high", "rationale": "checking prom",
                          "action_kind": "use_tool"}, org_id=org)
    await run_log.append(run_id, "observation",
                         {"tool": "query_prometheus", "ok": False,
                          "error": {"kind": "tool_error", "message": "scrape down"}}, org_id=org)
    await run_log.append(run_id, "assistant_turn",
                         {"hypothesis": "metric source down; read pod logs",
                          "rationale": "switching evidence family", "action_kind": "use_tool"},
                         org_id=org)
    await run_log.append(run_id, "observation",
                         {"tool": "list_pods", "ok": True, "preview": "OOMKilled"}, org_id=org)
    await run_log.append(run_id, "verification", {"verdict": "verified"}, org_id=org)
    await run_log.append(run_id, "run_finished", {"status": "answered"}, org_id=org)

    def _user():
        roles = ["sre"]
        return User(sub="u", username="op", email="o@x", name="Op", roles=roles,
                    display_roles=rbac.display_roles(roles), can_approve=rbac.can_approve(roles),
                    can_initiate=True, can_execute=False, org="o", org_id=org, user_id="u1")

    app.dependency_overrides[get_current_user] = _user
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.get(f"/runs/{run_id}/events")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert r.status_code == 200
    events = r.json()["events"]
    kinds = [e["kind"] for e in events]
    assert kinds == ["iteration_started", "assistant_turn", "observation",
                     "assistant_turn", "observation", "verification", "run_finished"]
    # the intelligence trail is visible: two DIFFERENT hypotheses across the failure
    hyps = [e["hypothesis"] for e in events if e["kind"] == "assistant_turn"]
    assert hyps[0] != hyps[1]
    # a failed then a successful observation
    obs = [e for e in events if e["kind"] == "observation"]
    assert obs[0]["ok"] is False and obs[0]["error"] == "tool_error"
    assert obs[1]["ok"] is True
    # CoT-safety: no event carries a raw-reasoning field
    assert all("chain_of_thought" not in e and "cot" not in e for e in events)
