"""Phase-1 honesty labels — no surface may claim something it didn't do.

P7 SRE remediation is "proposed, not executed" (never applied:True after only reading).
P8 policy checks are covered in test_templates.py (real predicate vs not-evaluated).
P9 the Traces tab returns NO fabricated spans — a deep-link to the real Langfuse trace instead.

Real implementations replace these labels in Phase 2 (U2 SRE actions, U1 policy predicates,
O1 real trace tree).
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.events import Emitter, RunChannel


class _CapEmitter(Emitter):
    """Captures emitted tokens/console lines so a test can assert what the user is told."""

    def __init__(self):
        super().__init__(RunChannel("honesty-run"))
        self.tokens: list[str] = []
        self.console_lines: list[str] = []

    async def token(self, text: str) -> None:  # type: ignore[override]
        self.tokens.append(text)

    async def console(self, stream: str, line: str) -> None:  # type: ignore[override]
        self.console_lines.append(line)


# ═══ P7 — SRE remediation is proposed, not executed ═══════════════════════════════════════════


class _NoopCG:
    """Async no-op context graph so this unit test doesn't require a live Neo4j."""

    def __init__(self, *a, **k):
        pass

    def __getattr__(self, _name):
        async def _noop(*a, **k):
            return None
        return _noop


class TestSreHonesty:
    async def test_sre_execute_never_claims_applied(self, monkeypatch):
        from app.agents import sre as sre_mod
        from app.agents.sre import sre_execute

        monkeypatch.setattr(sre_mod, "ContextGraph", _NoopCG)
        em = _CapEmitter()
        cfg = {"configurable": {"emitter": em}}
        state = {"run_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()),
                 "parsed_inputs": {"decision": {"action": "rollback", "target": "orders-api"}}}
        out = await sre_execute(state, cfg)
        oc = out["outcome"]
        assert oc["status"] == "proposed_not_executed"
        assert oc["applied"] is False, "SRE must never report applied:True for work it didn't do"
        # The user is told, in plain words, that nothing was executed.
        blob = " ".join(em.tokens).lower()
        assert "proposed" in blob and "not executed" in blob
        # tool_results, if present, must also carry applied:False (no fake success downstream).
        for tr in out.get("tool_results", []):
            assert tr.get("applied") is not True


# ═══ P9 — Traces tab: real deep-link, no fabricated spans ═════════════════════════════════════


class TestTracesHonesty:
    async def test_traces_returns_no_fake_spans_and_a_deep_link(self, live_db, org_id):
        from sqlalchemy import delete

        from app.api.artifacts import traces
        from app.db.models import Run
        from app.db.session import session_scope
        from app.schemas.auth import User as AuthUser

        async with session_scope() as s:
            run = Run(org_id=uuid.UUID(org_id), status="completed", mode="apply",
                      domain="cloudops", intent="create_s3", trace_id=None,
                      plan_json={"summary": {"add": 1}})
            s.add(run)
            await s.flush()
            rid = str(run.id)
        viewer = AuthUser(sub="t", username="viewer", org_id=org_id)
        try:
            data = await traces(rid, user=viewer)
            # O1: a run with no recorded steps still never fabricates spans — it falls back to
            # the honest note + Langfuse deep-link (and coming_soon is now retired).
            assert data["spans"] == [], "no steps recorded → no fabricated spans"
            assert data["coming_soon"] is False
            assert data["trace_id"] == rid           # trace_id == run_id
            assert "langfuse" in (data.get("message") or "").lower()
            # a deep-link is offered when a Langfuse host is configured
            if data.get("langfuse_host"):
                assert data["deep_link"] and rid in data["deep_link"]
        finally:
            async with session_scope() as s:
                await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
