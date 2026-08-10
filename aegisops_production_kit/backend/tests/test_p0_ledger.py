"""P0 authoritative-ledger tests: durable delivery semantics.

The property under test: a usage record must never silently disappear —
direct insert w/ bounded retry → fsync'd spill journal → idempotent replay.
True double-insert idempotency (ON CONFLICT on live PG) runs in the container tier."""

from __future__ import annotations

import json
import uuid
from datetime import UTC

import pytest

from app.integrations import usage_ledger
from app.settings import get_settings


@pytest.fixture
def spilled_settings(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "aegisops_ledger_spill_path", str(tmp_path / "spill.jsonl"))
    return s


def _row(settings, **kw):
    return usage_ledger.record_usage(
        settings, purpose=kw.pop("purpose", "classify"), model=kw.pop("model", "gemini-3.5-flash"),
        usage=kw.pop("usage", {"input": 100, "output": 20, "total": 120}), **kw)


# ── record shape / cost / secret hygiene ──────────────────────────────────────────────────

def test_record_carries_the_required_accounting_fields(spilled_settings):
    row = _row(spilled_settings, latency_ms=42)
    for key in ("id", "ts", "run_id", "task_id", "purpose", "provider", "model",
                "agent_kind", "prompt_version", "input_tokens", "output_tokens",
                "total_tokens", "cost_usd", "latency_ms", "outcome", "org_id"):
        assert key in row
    uuid.UUID(row["id"])  # client-generated, parseable
    assert row["input_tokens"] == 100 and row["total_tokens"] == 120
    assert row["cost_usd"] == pytest.approx(
        100 / 1e6 * spilled_settings.gemini_cost_per_1m_input
        + 20 / 1e6 * spilled_settings.gemini_cost_per_1m_output)


def test_sync_context_spills_immediately_and_without_content(spilled_settings):
    """No running loop (sync context) → the record goes straight to the durable journal.
    Spill rows carry ids/tokens/labels ONLY — never prompt or response content."""
    row = _row(spilled_settings)
    lines = usage_ledger.spill_path(spilled_settings).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    spilled = json.loads(lines[0])
    assert spilled["id"] == row["id"]
    for forbidden in ("prompt", "system", "content", "answer", "output_text"):
        assert forbidden not in spilled


def test_usage_key_spellings_are_normalized(spilled_settings):
    row = _row(spilled_settings, usage={"prompt_tokens": 7, "completion_tokens": 3})
    assert row["input_tokens"] == 7 and row["output_tokens"] == 3 and row["total_tokens"] == 10


def test_run_context_binding_attributes_spend(spilled_settings):
    usage_ledger.bind_run("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222")
    try:
        row = _row(spilled_settings)
        assert row["run_id"].startswith("11111111") and row["org_id"].startswith("22222222")
    finally:
        usage_ledger.bind_run(None, None)


# ── bounded retry → spill (never lost, never raises) ──────────────────────────────────────

async def test_transient_failure_retries_then_succeeds(monkeypatch, spilled_settings):
    calls = {"n": 0}

    async def flaky(row):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")

    sleeps: list[float] = []

    async def fake_sleep(d):
        sleeps.append(d)

    monkeypatch.setattr(usage_ledger, "_insert", flaky)
    monkeypatch.setattr(usage_ledger.asyncio, "sleep", fake_sleep)
    await usage_ledger._persist_with_retry(_row_dict(), spilled_settings)
    assert calls["n"] == 3
    assert not usage_ledger.spill_path(spilled_settings).exists(), "no spill on eventual success"
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0], "backoff grows between attempts"


async def test_final_failure_lands_in_the_spill_journal(monkeypatch, spilled_settings):
    async def always_down(row):
        raise RuntimeError("db down")

    async def fake_sleep(_):
        pass

    monkeypatch.setattr(usage_ledger, "_insert", always_down)
    monkeypatch.setattr(usage_ledger.asyncio, "sleep", fake_sleep)
    row = _row_dict()
    await usage_ledger._persist_with_retry(row, spilled_settings)
    lines = usage_ledger.spill_path(spilled_settings).read_text().splitlines()
    assert json.loads(lines[0])["id"] == row["id"], "the record survived — loudly, durably"


# ── replay: idempotent, partial-failure safe ──────────────────────────────────────────────

async def test_replay_moves_records_and_keeps_failures(monkeypatch, spilled_settings):
    ok_id, bad_id = str(uuid.uuid4()), str(uuid.uuid4())
    path = usage_ledger.spill_path(spilled_settings)
    path.write_text(
        json.dumps(_row_dict(id=ok_id)) + "\n" + json.dumps(_row_dict(id=bad_id)) + "\n")

    inserted: list[str] = []

    async def selective(row):
        if row["id"] == bad_id:
            raise RuntimeError("still failing")
        inserted.append(row["id"])

    monkeypatch.setattr(usage_ledger, "_insert", selective)
    out = await usage_ledger.replay_spill(spilled_settings)
    assert out == {"replayed": 1, "remaining": 1}
    assert inserted == [ok_id]
    remaining = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r["id"] for r in remaining] == [bad_id], "failed record stays for the next sweep"

    # Second sweep with the DB healthy: drains and removes the journal.
    async def healthy(row):
        inserted.append(row["id"])

    monkeypatch.setattr(usage_ledger, "_insert", healthy)
    out2 = await usage_ledger.replay_spill(spilled_settings)
    assert out2 == {"replayed": 1, "remaining": 0} and not path.exists()


def test_insert_statement_is_idempotent_by_construction():
    """Replay/retry safety at the SQL level: the insert is ON CONFLICT (id) DO NOTHING —
    a re-delivered record can never double-count. (Live double-insert proof: container
    tier, test_p0_ledger_live.)"""
    import inspect
    src = inspect.getsource(usage_ledger._insert)
    assert "on_conflict_do_nothing" in src and '"id"' in src


@pytest.mark.usefixtures("live_db")
async def test_live_double_insert_counts_once():
    from sqlalchemy import func, select

    from app.db.models import LlmUsage
    from app.db.session import session_scope

    row = _row_dict()
    await usage_ledger._insert(row)
    await usage_ledger._insert(row)  # identical id — must be a no-op
    async with session_scope() as s:
        n = (await s.execute(select(func.count()).select_from(LlmUsage)
                             .where(LlmUsage.id == uuid.UUID(row["id"])))).scalar_one()
    assert n == 1


def _row_dict(**kw):
    from datetime import datetime
    base = {
        "id": str(uuid.uuid4()), "ts": datetime.now(UTC).isoformat(),
        "run_id": None, "org_id": None, "task_id": None, "purpose": "classify",
        "provider": "google", "model": "gemini-3.5-flash", "requested_model": None,
        "agent_kind": "main", "prompt_version": None, "input_tokens": 1,
        "output_tokens": 1, "total_tokens": 2, "cost_usd": None, "latency_ms": 5,
        "outcome": "ok",
    }
    base.update(kw)
    return base
