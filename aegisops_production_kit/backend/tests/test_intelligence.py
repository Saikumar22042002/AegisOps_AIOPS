"""Intelligence-layer pins (Prompt 2, 2026-08-17).

Deterministic tests — no live Graphiti/LLM/DB. Every external leg is monkeypatched at its
seam so these pin the PIPELINE's behavior: planner source selection, no-retrieval fast
path, dedup, budgets, typed blocks, provider-neutral adapters, temporal parsing, and the
deterministic fact diffing that feeds Graphiti from the immutable journal.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.intelligence import compaction, facts, graphiti_layer, pipeline


# ── identity / temporal primitives ─────────────────────────────────────────────────────────

def test_stable_uuid_is_deterministic_and_tenant_scoped():
    a = graphiti_layer.stable_uuid("org1", "port", "aws", "MySource", "8501")
    b = graphiti_layer.stable_uuid("org1", "port", "aws", "MySource", "8501")
    c = graphiti_layer.stable_uuid("org2", "port", "aws", "MySource", "8501")
    assert a == b and a != c


def test_utc_coercion_handles_naive_datetimes():
    naive = datetime(2026, 8, 17, 10, 0, 0)
    out = graphiti_layer._utc(naive)
    assert out.tzinfo is not None and out.hour == 10
    assert graphiti_layer._utc(None) is None


def test_temporal_window_parsing():
    w = pipeline._temporal_window("what did I create yesterday?")
    assert w and (w[1] - w[0]).total_seconds() == 86400
    assert pipeline._temporal_window("describe MySource") is None


def test_ports_diffing_reads_both_state_shapes():
    assert facts._ports({"attributes": {"ingress_ports": [80, 8501]}}) == {80, 8501}
    assert facts._ports({"ingress_ports": ["443"]}) == {443}
    assert facts._ports(None) == set()
    assert facts._ports({"attributes": {}}) == set()


# ── the no-retrieval fast path (test 15: a simple message pays no retrieval tax) ──────────

async def test_greeting_skips_without_touching_the_gate(monkeypatch):
    from app.harness import memory as harness_memory

    async def _boom(*a, **k):  # the gate must NOT be called
        raise AssertionError("gate was called for a greeting")

    monkeypatch.setattr(harness_memory, "gate", _boom)
    bundle = await pipeline.assemble(object(), message="hi!", org_id="o", session_id=None)
    assert bundle.text == "" and bundle.trace.skipped
    assert "deterministic" in bundle.trace.skip_reason
    assert bundle.trace.sources == {}  # no telemetry for sources never queried


async def test_parameter_answer_skips(monkeypatch):
    bundle = await pipeline.assemble(object(), message="t3.micro, create, none",
                                     org_id="o", session_id="s")
    assert bundle.trace.skipped and bundle.text == ""


# ── gate honored (P2 seam live) ────────────────────────────────────────────────────────────

async def test_gate_skip_stops_retrieval(monkeypatch):
    from app.harness import memory as harness_memory

    async def _no(settings, message, *, run_id=None, org_id=None):
        return harness_memory.GateDecision(retrieve=False, query=message, reason="not needed")

    monkeypatch.setattr(harness_memory, "gate", _no)
    bundle = await pipeline.assemble(object(), message="what is a vpc peering?",
                                     org_id="o", session_id=None)
    assert bundle.trace.skipped and bundle.trace.gate["retrieve"] is False


# ── planner + assembly (typed blocks, dedup, budget, per-leg fault tolerance) ─────────────

@pytest.fixture
def _wired(monkeypatch):
    from app.harness import memory as harness_memory

    async def _yes(settings, message, *, run_id=None, org_id=None):
        return harness_memory.GateDecision(retrieve=True, query=message, reason="test")

    async def _entities(org_id, message):
        return ["MySource"] if "mysource" in message.lower() else []

    async def _revs(org_id, *, names, window, limit=6):
        return ["2026-08-17 03:50 UTC — maya modified MySource (aws ec2) · run 77d1e779"]

    async def _gfacts(org_id, query, *, num_results=8, valid_at=None,
                      include_invalidated=False, settings=None):
        return [{"fact": "inbound TCP port 8501 is open on MySource", "uuid": "u1",
                 "valid_at": datetime(2026, 8, 17, tzinfo=timezone.utc),
                 "invalid_at": None, "expired_at": None, "attributes": {}, "current": True},
                {"fact": "inbound TCP port 8501 is open on MySource", "uuid": "dup",
                 "valid_at": None, "invalid_at": None, "expired_at": None,
                 "attributes": {}, "current": True}]

    async def _eps(org_id, *, last_n=3, settings=None):
        return ["(2026-08-16 17:00) created MySource and opened port 8501"]

    async def _msgs(session_id, query, *, k=3, settings=None):
        return [{"role": "user", "content": "open port 8501 on MySource"}]

    async def _accepted(org_id, limit=4):
        return []

    monkeypatch.setattr(harness_memory, "gate", _yes)
    monkeypatch.setattr(pipeline, "_known_entities", _entities)
    monkeypatch.setattr(pipeline, "_fetch_revisions", _revs)
    monkeypatch.setattr(pipeline, "_fetch_accepted_facts", _accepted)
    monkeypatch.setattr(graphiti_layer, "search_facts", _gfacts)
    monkeypatch.setattr(graphiti_layer, "recent_episodes", _eps)
    from app.agents import memory as conv_memory
    monkeypatch.setattr(conv_memory, "retrieve", _msgs)


async def test_entity_question_gets_typed_infra_blocks(_wired):
    bundle = await pipeline.assemble(object(), message="What changed on MySource?",
                                     org_id="o", session_id="s")
    assert "[CHANGE HISTORY" in bundle.text and "[INFRASTRUCTURE MEMORY" in bundle.text
    assert bundle.trace.entities == ["MySource"]
    assert bundle.trace.sources["revisions"].queried
    assert bundle.trace.sources["graphiti_facts"].selected == 1  # the dup was deduplicated
    # No episodes leg for a non-recall question — the planner is selective.
    assert "graphiti_episodes" not in bundle.trace.sources


async def test_recall_question_pulls_episodes(_wired):
    bundle = await pipeline.assemble(object(), message="what did we discuss earlier about MySource?",
                                     org_id="o", session_id="s")
    assert "[PAST SESSIONS" in bundle.text
    assert bundle.trace.sources["graphiti_episodes"].selected == 1


async def test_budget_drops_are_counted(_wired, monkeypatch):
    async def _many(org_id, *, names, window, limit=6):
        return [f"2026-08-17 line {i} " + "x" * 300 for i in range(30)]

    monkeypatch.setattr(pipeline, "_fetch_revisions", _many)
    bundle = await pipeline.assemble(object(), message="What changed on MySource?",
                                     org_id="o", session_id="s", max_chars=900)
    assert bundle.trace.dropped_by_budget > 0
    assert len(bundle.text) <= 1400  # hard budget holds (headers included)


async def test_failing_source_degrades_not_fails(_wired, monkeypatch):
    async def _boom(org_id, query, **kw):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(graphiti_layer, "search_facts", _boom)
    bundle = await pipeline.assemble(object(), message="What changed on MySource?",
                                     org_id="o", session_id="s")
    assert bundle.trace.sources["graphiti_facts"].error  # honest per-leg failure
    assert "[CHANGE HISTORY" in bundle.text              # other legs still delivered


# ── provider-neutral adapters (multi-LLM requirement) ─────────────────────────────────────

async def test_graphiti_llm_adapter_routes_through_p1_purpose(monkeypatch):
    pytest.importorskip("graphiti_core")
    from app.llm import service as llm_service
    captured = {}

    async def _fake_classify(settings, system, prompt, *, purpose, response_schema=None, **kw):
        captured["purpose"] = purpose
        return {"is_duplicate": False}

    monkeypatch.setattr(llm_service, "classify_json", _fake_classify)
    from pydantic import BaseModel

    class _M(BaseModel):
        is_duplicate: bool

    client = graphiti_layer._build_llm_client(object())
    from graphiti_core.prompts.models import Message
    out = await client._generate_response([Message(role="system", content="s"),
                                           Message(role="user", content="u")], _M)
    assert out == {"is_duplicate": False}
    assert captured["purpose"] == "consolidation"  # catalog/bindings decide the provider


async def test_reranker_degrades_to_input_order_when_embeddings_fail(monkeypatch):
    from app.llm import service as llm_service

    async def _fail(settings, texts):
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(llm_service, "embed", _fail)
    rr = graphiti_layer.AegisReranker(object())
    ranked = await rr.rank("q", ["a", "b", "c"])
    assert [p for p, _ in ranked] == ["a", "b", "c"]  # stated degradation, not silent reorder


async def test_embedder_adapter_batches_through_service(monkeypatch):
    from app.llm import service as llm_service

    async def _embed(settings, texts):
        return [[0.1, 0.2]] * len(texts)

    monkeypatch.setattr(llm_service, "embed", _embed)
    emb = graphiti_layer.AegisEmbedder(object())
    assert await emb.create("hello") == [0.1, 0.2]
    assert len(await emb.create_batch(["a", "b"])) == 2


# ── compaction (deterministic, empty-session honest) ───────────────────────────────────────

async def test_compaction_empty_session_returns_nothing():
    assert await compaction.session_state_block("not-a-uuid", "also-not") == ""
    assert await compaction.session_state_block(
        "2f1a9c66-8a1a-4bde-9c3e-000000000001", None) == ""


# ── graphiti off/unreachable ⇒ graceful degradation (test 16) ─────────────────────────────

async def test_search_facts_returns_empty_when_layer_off(monkeypatch):
    async def _none(settings=None):
        return None

    monkeypatch.setattr(graphiti_layer, "get_graphiti", _none)
    assert await graphiti_layer.search_facts("o", "q") == []
    assert await graphiti_layer.recent_episodes("o") == []
    assert (await graphiti_layer.stats("o"))["available"] is False
