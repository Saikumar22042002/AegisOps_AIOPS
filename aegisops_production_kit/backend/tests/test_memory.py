"""Phase-A MEMORY & CONTINUITY (03_TEST_MATRIX §D — guards N-03).

Screenshots 16/18: the assistant answered "this is the beginning of our conversation" /
"my context window is currently blank" because agents received ONLY the current message.
Target API (Phase B): `app.agents.memory` —

  build_transcript(session_id, max_chars=…) -> str   # "" when no history; recent window +
                                                     # older-topics digest for long threads
  prior_user_questions(session_id) -> list[str]      # ordered user turns

Uses the real Postgres (integration tier).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Message, Session
from app.db.session import session_scope

_Q1 = "How many VMs are running in AWS?"
_Q2 = "Create an S3 bucket named memcheck-bucket"
_Q3 = "What is the VPC id of sai-test?"


async def _make_session(org_id: str, turns: list[tuple[str, str]]) -> str:
    # Explicit increasing timestamps: rows inserted in one transaction would otherwise share
    # the same server-side now(), making chronological ordering nondeterministic.
    t0 = datetime.now(timezone.utc) - timedelta(minutes=len(turns) + 1)
    async with session_scope() as s:
        sess = Session(org_id=uuid.UUID(org_id), title="memory-itest")
        s.add(sess)
        await s.flush()
        sid = str(sess.id)
        for i, (role, content) in enumerate(turns):
            s.add(Message(org_id=uuid.UUID(org_id), session_id=sess.id, role=role,
                          content=content, created_at=t0 + timedelta(seconds=i)))
    return sid


@pytest.fixture
async def convo(org_id):
    sid = await _make_session(org_id, [
        ("user", _Q1), ("assistant", "3 running instances."),
        ("user", _Q2), ("assistant", "Planned aws.s3 — awaiting approval."),
        ("user", _Q3), ("assistant", "vpc-0d22ef2487a3ae2d6."),
    ])
    yield sid
    from sqlalchemy import delete
    async with session_scope() as s:
        await s.execute(delete(Message).where(Message.session_id == uuid.UUID(sid)))
        await s.execute(delete(Session).where(Session.id == uuid.UUID(sid)))


async def test_transcript_contains_all_prior_turns(convo):
    from app.agents import memory
    t = await memory.build_transcript(convo)
    for q in (_Q1, _Q2, _Q3):
        assert q in t, f"transcript missing user turn: {q!r}"
    assert "vpc-0d22ef2487a3ae2d6" in t          # assistant turns included too
    assert t.find(_Q1) < t.find(_Q2) < t.find(_Q3)  # chronological


async def test_prior_user_questions_ordered(convo):
    from app.agents import memory
    qs = await memory.prior_user_questions(convo)
    assert qs == [_Q1, _Q2, _Q3]


async def test_empty_session_yields_empty_transcript(org_id):
    from app.agents import memory
    sid = await _make_session(org_id, [])
    assert await memory.build_transcript(sid) == ""


async def test_long_thread_keeps_early_facts_within_budget(org_id):
    """> N turns: early questions must still be recallable (older-topics digest), and the
    result must respect the char budget — never 'my context window is blank'."""
    from app.agents import memory
    turns: list[tuple[str, str]] = [("user", "My project codename is BLUEFALCON, remember it."),
                                    ("assistant", "Noted.")]
    for i in range(40):
        turns.append(("user", f"Filler question number {i} about capacity planning " + "x" * 200))
        turns.append(("assistant", "Filler answer " + "y" * 200))
    sid = await _make_session(org_id, turns)
    t = await memory.build_transcript(sid, max_chars=6000)
    assert len(t) <= 7000                          # budget respected (small formatting slack)
    assert "BLUEFALCON" in t                       # earliest fact survives via the digest
    assert "Filler question number 39" in t        # newest turns fully present


async def test_general_agent_prompt_includes_history(convo, monkeypatch):
    """The general agent must send the transcript to the LLM — the exact defect of
    screenshots 16/18. We capture what stream_answer receives instead of calling Gemini."""
    from app.agents import general, llm
    from app.agents.events import Emitter, RunChannel

    seen: dict = {}

    async def fake_stream(settings, system, prompt, emitter, **kw):
        seen["system"] = system
        seen["prompt"] = prompt
        await emitter.token("ok")
        return "ok"

    monkeypatch.setattr(llm, "stream_answer", fake_stream)
    monkeypatch.setattr(general.llm, "stream_answer", fake_stream, raising=False)
    ch = RunChannel("mem-run")
    state = {"message": "What is my previous question to you?", "session_id": convo,
             "org_id": "00000000-0000-0000-0000-000000000000", "run_id": "mem-run",
             "intent_confidence": 0.9, "routing_reason": "test"}
    out = await general.general(state, {"configurable": {"emitter": Emitter(ch)}})
    assert out["answer"]
    blob = (seen.get("system") or "") + (seen.get("prompt") or "")
    for q in (_Q1, _Q2, _Q3):
        assert q in blob, f"general agent never saw prior turn: {q!r}"


# ═══ M1/M2/M3 — Context Engine: positional + semantic recall, build_context ═══════════════════
# Headline (fix-doc M2): in a 100-message thread, "what was my 20th question?" returns turn 20
# verbatim. Positional recall is deterministic (no embeddings); semantic degrades to keyword.

async def _make_100_turn_session(org_id: str) -> str:
    turns = []
    for i in range(1, 51):
        turns.append(("user", f"Question number {i}: how do I configure widget {i}?"))
        turns.append(("assistant", f"Answer to number {i}."))
    return await _make_session(org_id, turns)


@pytest.fixture
async def big_convo(org_id):
    sid = await _make_100_turn_session(org_id)
    yield sid
    from sqlalchemy import delete
    async with session_scope() as s:
        await s.execute(delete(Message).where(Message.session_id == uuid.UUID(sid)))
        await s.execute(delete(Session).where(Session.id == uuid.UUID(sid)))


async def test_get_turn_returns_the_20th_question_verbatim(big_convo):
    from app.agents import memory
    turn = await memory.get_turn(big_convo, 20, role="user")
    assert turn is not None
    assert turn["content"] == "Question number 20: how do I configure widget 20?"
    assert turn["role"] == "user" and turn["ordinal"] == 20


def test_detect_recall_parses_positional_queries():
    from app.agents import memory
    assert memory.detect_recall("what was my 20th question?") == (20, "user")
    assert memory.detect_recall("show me the 3rd message") == (3, "user")
    assert memory.detect_recall("the first question") == (1, "user")
    assert memory.detect_recall("provision an EC2 instance") is None  # not a recall


def test_detect_recall_parses_turn_n_shape():
    """Gate finding (2026-07-12): the natural 'turn 20' phrasing matched neither noun-last form."""
    from app.agents import memory
    assert memory.detect_recall("What did I say in turn 20?") == (20, "user")
    assert memory.detect_recall("turn 7") == (7, "user")
    assert memory.detect_recall("show me message #7") == (7, "user")
    # Guard against noun-first false positives on ordinary sentences.
    assert memory.detect_recall("I request 3 VMs") is None
    assert memory.detect_recall("question 5 of the quiz is hard") is None


async def test_build_context_includes_the_20th_turn_verbatim(big_convo):
    from app.agents import memory
    ctx = await memory.build_context(big_convo, purpose="general",
                                     current_message="what was my 20th question?")
    assert "Question number 20: how do I configure widget 20?" in ctx, \
        "the recall query must surface turn 20's full text (M2 positional recall)"


async def test_semantic_recall_finds_a_turn_by_content_keyword_fallback(big_convo):
    # No Gemini key here → retrieve() uses pg_trgm keyword similarity. A distinctive query should
    # surface the matching earlier turn.
    from app.agents import memory
    hits = await memory.retrieve(big_convo, "configure widget 37", k=3)
    assert any("widget 37" in h["content"] for h in hits), \
        "keyword retrieval must find the relevant earlier turn by content"


async def test_build_context_never_claims_no_history(big_convo):
    from app.agents import memory
    ctx = await memory.build_context(big_convo, purpose="router",
                                     current_message="do that again")
    assert ctx.strip(), "context must be non-empty for a populated session"


async def test_general_answers_positional_recall_without_the_llm(big_convo, monkeypatch):
    """M2: an exact positional-recall query is answered verbatim from the store — the LLM is
    NOT invoked (deterministic, un-hallucinatable), so it works even when the LLM is down."""
    from app.agents import general as gen
    from app.agents.events import Emitter, RunChannel

    async def _boom(*a, **k):
        raise AssertionError("exact recall must not call the LLM")

    monkeypatch.setattr(gen.llm, "stream_answer", _boom)
    state = {"run_id": "r", "org_id": "o", "session_id": big_convo,
             "message": "what was my 20th question?"}
    out = await gen.general(state, {"configurable": {"emitter": Emitter(RunChannel("r"))}})
    assert "Question number 20: how do I configure widget 20?" in out["answer"]


# ═══ M5 — context offloading: plans live in the store, referenced not inlined ═════════════════

async def test_context_stays_in_budget_and_never_inlines_plan_json(org_id):
    """A long session with many plan-bearing runs stays within the context budget — the large
    plan JSON lives in runs.plan_json, never inlined into the transcript."""
    from app.agents import memory
    # Simulate assistant turns that mention plans WITHOUT embedding raw plan JSON.
    turns = []
    for i in range(30):
        turns.append(("user", f"create resource {i}"))
        turns.append(("assistant", memory.plan_ref_line(f"run-{i}", {"summary": {"add": 3}})))
    sid = await _make_session(org_id, turns)
    try:
        ctx = await memory.build_context(sid, purpose="cloudops", current_message="what next?")
        assert len(ctx) <= 3000 * 1.2, "context must stay within the purpose budget"
        assert "resource_changes" not in ctx and "after_unknown" not in ctx, \
            "raw plan JSON must never be inlined into the LLM context"
    finally:
        from sqlalchemy import delete
        async with session_scope() as s:
            await s.execute(delete(Message).where(Message.session_id == uuid.UUID(sid)))
            await s.execute(delete(Session).where(Session.id == uuid.UUID(sid)))


async def test_fetch_plan_returns_stored_plan_on_demand(org_id):
    """An agent asked about a prior plan fetches it from the store — not a truncated inline copy."""
    import uuid as _uuid

    from sqlalchemy import delete

    from app.agents import memory
    from app.db.models import Run

    plan = {"summary": {"add": 8, "change": 0, "destroy": 0},
            "diff": [{"address": f"r{i}", "actions": ["create"]} for i in range(8)]}
    async with session_scope() as s:
        run = Run(org_id=_uuid.UUID(org_id), status="completed", mode="apply", plan_json=plan)
        s.add(run)
        await s.flush()
        rid = str(run.id)
    try:
        fetched = await memory.fetch_plan(rid)
        assert fetched and fetched["summary"]["add"] == 8 and len(fetched["diff"]) == 8
        ref = memory.plan_ref_line(rid, plan)
        assert "+8" in ref and "full plan available" in ref and "diff" not in ref
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == _uuid.UUID(rid)))
