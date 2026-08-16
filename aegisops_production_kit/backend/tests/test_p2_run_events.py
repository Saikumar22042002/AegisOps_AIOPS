"""P2.5 — durable run log: gapless seq, redaction-at-write, replay (06 §8.2, 10 §0).

DB-free pins cover the writer's guards; the gapless-seq + replay round-trip is
integration-tier (real PostgreSQL, the UNIQUE(run_id, seq) constraint is the point).
"""

from __future__ import annotations

import uuid

import pytest

from app.harness import run_log

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_kind_enum_is_the_18_of_06_and_includes_agent_gate():
    assert len(run_log.KINDS) == 18
    assert "agent_gate" in run_log.KINDS            # C-05 resolution
    assert {"iteration_started", "assistant_turn", "tool_call", "observation",
            "verification", "run_finished", "budget"} <= run_log.KINDS


async def test_unknown_kind_is_a_loud_error(monkeypatch):
    async def _never(*a, **k):
        raise AssertionError("must not reach the DB")
    monkeypatch.setattr("app.harness.run_log.session_scope", _never)
    with pytest.raises(ValueError, match="unknown run-event kind"):
        await run_log.append(str(uuid.uuid4()), "not_a_kind", {})


# ── integration: the gapless invariant + redaction + replay ─────────────────────────────────

@pytest.mark.usefixtures("live_db")
async def test_gapless_seq_redaction_and_replay():
    from sqlalchemy import select

    from app.db.models import RunEvent
    from app.db.session import session_scope

    run_id = str(uuid.uuid4())
    await run_log.append(run_id, "iteration_started", {"n": 1})
    await run_log.append(run_id, "assistant_turn",
                         {"hypothesis": "h", "token": "sk-live-SECRET-should-redact"})
    await run_log.append(run_id, "observation", {"ok": True})
    await run_log.append(run_id, "run_finished", {"status": "answered"})

    events = await run_log.replay(run_id)
    seqs = [e.seq for e in events]
    assert seqs == [0, 1, 2, 3]                      # gapless, monotonic (10 §0 invariant 1)
    assert [e.kind for e in events] == ["iteration_started", "assistant_turn",
                                        "observation", "run_finished"]
    # redaction-at-write: the secret never reaches the durable row
    turn = next(e for e in events if e.kind == "assistant_turn")
    assert "sk-live-SECRET" not in str(turn.payload)

    # a second run's seq is independent (per-run monotonic, not global)
    other = str(uuid.uuid4())
    assert await run_log.append(other, "iteration_started", {"n": 1}) == 0
    async with session_scope() as s:
        rows = (await s.execute(select(RunEvent).where(
            RunEvent.run_id == uuid.UUID(run_id)))).scalars().all()
    assert len(rows) == 4
