"""P2.3/P2.6 — memory lifecycle: retrieval gate (fail-open, overrides, observable) and
consolidation-to-proposals (proposals-only, human-accept, supersede-not-coexist)."""

from __future__ import annotations

import pytest

from app.harness import memory
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _capture_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    async def _append(run_id, kind, payload, org_id=None):
        events.append((kind, payload))
        return len(events) - 1
    monkeypatch.setattr("app.harness.memory.run_log.append", _append)
    return events


# ── retrieval gate ──────────────────────────────────────────────────────────────────────────

async def test_gate_deterministic_override_forces_retrieve(monkeypatch, _capture_events):
    async def _never(*a, **k):
        raise AssertionError("override must not call the model")
    monkeypatch.setattr(memory.service, "classify_json", _never)
    d = await memory.gate(Settings(), "what did you say last time about the db?", run_id="r1")
    assert d.retrieve and d.forced and "override" in d.reason
    assert ("agent_gate", ) in [(k,) for k, _ in _capture_events]  # observable


async def test_gate_model_decides_skip_and_is_observable(monkeypatch, _capture_events):
    async def _skip(*a, **k):
        return {"retrieve": False, "reason": "self-contained"}
    monkeypatch.setattr(memory.service, "classify_json", _skip)
    d = await memory.gate(Settings(), "what is 2+2?", run_id="r1")
    assert d.retrieve is False
    gate_ev = [p for k, p in _capture_events if k == "agent_gate"][0]
    assert gate_ev["skipped"] is True and gate_ev["retrieve"] is False


async def test_gate_fails_open_on_model_error(monkeypatch, _capture_events):
    from app.llm.errors import ModelError

    async def _boom(*a, **k):
        raise ModelError("unavailable", "down")
    monkeypatch.setattr(memory.service, "classify_json", _boom)
    d = await memory.gate(Settings(), "some ordinary question", run_id="r1")
    assert d.retrieve is True and d.forced and "fail open" in d.reason


# ── consolidation ───────────────────────────────────────────────────────────────────────────

async def test_consolidation_produces_proposals_only(monkeypatch):
    async def _c(*a, **k):
        return {"facts": [{"subject": "vpc", "content": "prod VPC is vpc-123 in us-east-1"},
                          {"content": "team prefers blue/green deploys"}],
                "episode": "Investigated a latency spike; found an OOM in api-x."}
    monkeypatch.setattr(memory.service, "classify_json", _c)
    props = await memory.consolidate(Settings(), run_id="r1", transcript="...")
    assert [p.kind for p in props] == ["fact", "fact", "episode"]
    assert all(p.origin_run_id == "r1" for p in props)
    # proposals carry no DB identity — nothing was written (write path is human-accept only)
    assert all(not hasattr(p, "id") for p in props)


async def test_consolidation_failure_is_empty_never_raises(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(memory.service, "classify_json", _boom)
    assert await memory.consolidate(Settings(), run_id="r1", transcript="x") == []


# ── acceptance write path (integration: real PostgreSQL) ────────────────────────────────────

@pytest.mark.usefixtures("live_db")
async def test_accept_writes_and_supersede_does_not_coexist(throwaway_org):
    from sqlalchemy import select

    from app.db.models import MemoryItem
    from app.db.session import session_scope

    org = throwaway_org
    p1 = memory.MemoryProposal(kind="fact", subject="region",
                               content="prod is us-east-1", origin_run_id=None)
    id1 = await memory.accept_proposal(Settings(), p1, org_id=org, accepted_by="op")
    p2 = memory.MemoryProposal(kind="fact", subject="region",
                               content="prod moved to us-west-2", origin_run_id=None)
    id2 = await memory.accept_proposal(Settings(), p2, org_id=org, accepted_by="op",
                                       supersedes=id1)
    async with session_scope() as s:
        old = await s.get(MemoryItem, id1)
        new = await s.get(MemoryItem, id2)
        active = (await s.execute(select(MemoryItem).where(
            MemoryItem.org_id.isnot(None), MemoryItem.subject == "region",
            MemoryItem.status == "active", MemoryItem.id.in_((id1, id2))))).scalars().all()
    assert old.status == "superseded" and new.status == "active"   # supersede, not coexist
    assert new.provenance == "consolidation_accepted" and new.supersedes == id1
    assert [m.id for m in active] == [id2]
