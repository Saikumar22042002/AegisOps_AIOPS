"""O3 — metrics hygiene + SSE exempt from the per-IP rate limiter.

Every declared metric must be a live series or gone (no always-empty series lying on the
dashboard), and the long-lived SSE endpoints must not be throttled by the request limiter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import metrics as metrics_mod
from app.api.chat import _record_approval_wait
from app.metrics import REGISTRY
from app.ratelimit import limiter


def test_sse_endpoints_are_exempt_from_rate_limit():
    exempt = limiter._exempt_routes
    assert "app.api.chat.chat" in exempt          # POST /chat (SSE)
    assert "app.api.chat.chat_stream" in exempt   # GET /chat/stream/{run_id} (SSE reconnect)
    # A normal write endpoint is NOT exempt — the limiter still protects it.
    assert "app.api.chat.resolve_approval" not in exempt


def test_tool_retries_metric_is_removed_not_left_empty():
    # It was declared but never incremented — an always-empty series. Removed, not faked.
    assert not hasattr(metrics_mod, "TOOL_RETRIES")
    assert REGISTRY.get_sample_value("aegisops_tool_retries_total", {"tool": "terraform"}) is None


def test_revived_metric_families_are_registered():
    names = {m.name for m in REGISTRY.collect()}
    # prometheus_client strips the _seconds suffix from the family name for histograms.
    assert "aegisops_agent_step_duration_seconds" in names
    assert "aegisops_approval_wait_seconds" in names


def test_approval_wait_records_a_real_sample():
    labels = {"domain": "cloudops", "decision": "approved"}
    name = "aegisops_approval_wait_seconds_count"
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    # Paused at the gate 42s ago → a real, positive wait is observed.
    started = datetime.now(timezone.utc) - timedelta(seconds=42)
    _record_approval_wait("cloudops", "approved", started)
    after = REGISTRY.get_sample_value(name, labels)
    assert after == before + 1
    total = REGISTRY.get_sample_value("aegisops_approval_wait_seconds_sum", labels)
    assert total and total > 0  # a positive wall-clock wait, not zero


def test_approval_wait_skips_when_no_start_recorded():
    labels = {"domain": "sre", "decision": "rejected"}
    name = "aegisops_approval_wait_seconds_count"
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    _record_approval_wait("sre", "rejected", None)  # legacy run, no approval step
    after = REGISTRY.get_sample_value(name, labels) or 0.0
    assert after == before  # nothing fabricated


async def test_agent_step_duration_observed_on_end_step(live_db, org_id):
    """The previously-dead AGENT_STEP_DURATION series is now populated by real step timing."""
    import uuid

    from sqlalchemy import delete

    from app.agents import timing
    from app.db.models import Run
    from app.db.session import session_scope

    labels = {"agent": "core", "step": "policy_evaluation"}
    name = "aegisops_agent_step_duration_seconds_count"
    before = REGISTRY.get_sample_value(name, labels) or 0.0
    async with session_scope() as s:
        run = Run(org_id=uuid.UUID(org_id), status="running", mode="apply")
        s.add(run)
        await s.flush()
        rid = str(run.id)
    try:
        await timing.start_step(rid, "policy_evaluation")
        await timing.end_step(rid, "policy_evaluation", status="done")
        after = REGISTRY.get_sample_value(name, labels)
        assert after == before + 1  # a real per-step latency sample was recorded
    finally:
        async with session_scope() as s:
            await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
