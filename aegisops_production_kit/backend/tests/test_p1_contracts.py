"""P1.1 — canonical contract pins (Redesign/05 §11).

These are the provider-neutral shapes every adapter and the P2 harness build on.
If one of these tests has to change, that is a contract change — record it in
Redesign/11 before merging (additive-only until the C-03 versioning decision).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.llm.errors import FAILOVER, RETRIABLE, ModelError, classify_status
from app.llm.types import (
    GOVERNED_PURPOSES,
    PURPOSES,
    CanonicalMessage,
    ModelRequest,
    ModelResponse,
    RoutePlan,
    ServedBy,
    StreamEvent,
    ToolCall,
    ToolResult,
    Usage,
    args_hash,
)

# ── construction + validation ────────────────────────────────────────────────────────────────

def test_purposes_and_governed_set_match_04():
    assert set(GOVERNED_PURPOSES) == {"router", "planner", "loop.main", "judge"}
    assert GOVERNED_PURPOSES <= set(PURPOSES)
    assert "embeddings" in PURPOSES  # 768-d pgvector pin lives on this purpose


def test_request_requires_known_purpose_and_messages():
    with pytest.raises(ValidationError):
        ModelRequest(purpose="nope", messages=[CanonicalMessage(role="user", content="x")])
    with pytest.raises(ValidationError):
        ModelRequest(purpose="general", messages=[])
    req = ModelRequest(purpose="general",
                       messages=[CanonicalMessage(role="user", content="hi")])
    assert req.timeout_s == 120.0  # 07 P1.2 default
    assert ModelRequest(purpose="general", params={"timeout_s": 5},
                        messages=req.messages).timeout_s == 5.0


def test_message_role_shape_is_enforced():
    with pytest.raises(ValidationError):  # tool_calls only on assistant
        CanonicalMessage(role="user", content="x",
                         tool_calls=[ToolCall(id="1", name="t", arguments={})])
    with pytest.raises(ValidationError):  # tool messages must answer a call
        CanonicalMessage(role="tool", content="result")
    ok = CanonicalMessage(role="tool", content="result", tool_call_id="1")
    assert ok.tool_call_id == "1"


def test_extra_fields_are_rejected_everywhere():
    for cls, kw in ((Usage, {"input_tokens": 1}), (ServedBy, {"provider": "p", "model": "m"})):
        with pytest.raises(ValidationError):
            cls(**kw, smuggled="x")


# ── tool call / result ───────────────────────────────────────────────────────────────────────

def test_tool_call_args_hash_is_canonical_and_order_independent():
    a = ToolCall(id="1", name="t", arguments={"b": 2, "a": 1})
    b = ToolCall(id="2", name="t", arguments={"a": 1, "b": 2})
    assert a.args_hash == b.args_hash == args_hash({"a": 1, "b": 2})
    assert a.args_hash != ToolCall(id="3", name="t", arguments={"a": 1}).args_hash


def test_failed_tool_result_must_carry_error():
    with pytest.raises(ValidationError):
        ToolResult(tool_call_id="1", ok=False)
    r = ToolResult(tool_call_id="1", ok=False,
                   error={"kind": "timeout", "message": "30s"}, stage="timeout")
    assert r.stage == "timeout"


# ── serialization round-trip ─────────────────────────────────────────────────────────────────

def test_response_round_trips_through_json():
    resp = ModelResponse(
        content="hello", finish_reason="tool_calls",
        tool_calls=[ToolCall(id="1", name="ns.read", arguments={"q": "x"})],
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
        served_by=ServedBy(provider="google", model="gemini-3.5-flash",
                           requested_model="gemini-3.5-flash", fallback_hop=0),
        latency_ms=42)
    again = ModelResponse.model_validate_json(resp.model_dump_json())
    assert again == resp
    assert again.usage.as_ledger() == {"input": 10, "output": 5, "total": 15}


def test_invalid_payload_rejected_on_deserialize():
    with pytest.raises(ValidationError):
        ModelResponse.model_validate({"content": "x", "finish_reason": "banana",
                                      "served_by": {"provider": "p", "model": "m"}})


# ── streaming ────────────────────────────────────────────────────────────────────────────────

def test_stream_event_shapes():
    assert StreamEvent(kind="text_delta", payload={"text": "hi"}).payload["text"] == "hi"
    with pytest.raises(ValidationError):
        StreamEvent(kind="text_delta", payload={})
    with pytest.raises(ValidationError):
        StreamEvent(kind="error", payload={"message": "no kind"})
    done = StreamEvent(kind="done")
    assert done.payload == {}


# ── routing ──────────────────────────────────────────────────────────────────────────────────

def test_route_plan_validates_purpose_and_carries_fallbacks():
    rp = RoutePlan(purpose="knowledge", provider="google", model="gemini-3.5-flash",
                   fallbacks=[("google", "gemini-flash-latest")], pinned_by="binding")
    assert rp.fallbacks[0] == ("google", "gemini-flash-latest")
    with pytest.raises(ValidationError):
        RoutePlan(purpose="not-a-purpose", provider="google", model="m")


# ── errors ───────────────────────────────────────────────────────────────────────────────────

def test_error_taxonomy_retry_and_failover_semantics():
    assert ModelError("context_overflow", "too big").retriable is False   # compact, not retry
    assert ModelError("context_overflow", "too big").failover is False    # NEVER failover
    assert ModelError("upstream_rate_limited", "429").retriable is True
    assert ModelError("upstream_rate_limited", "429").failover is True
    assert ModelError("auth_permanent", "revoked").failover is True
    assert ModelError("invalid_request", "bad").retriable is False
    assert RETRIABLE.isdisjoint({"invalid_request", "content_filtered", "refusal"})
    assert "context_overflow" not in FAILOVER


def test_error_redacts_secrets_and_maps_status():
    e = ModelError("auth_permanent", "denied key=sk-live-abc123 for call")
    assert "sk-live-abc123" not in str(e) and "[redacted]" in str(e)
    assert classify_status(429, "slow down").kind == "upstream_rate_limited"
    assert classify_status(529, "overloaded").kind == "upstream_rate_limited"
    assert classify_status(401, "bad key").kind == "auth_permanent"
    assert classify_status(503, "boom").kind == "unavailable"
    assert classify_status(400, "bad req").kind == "invalid_request"
    assert classify_status(504, "gw timeout", retry_after_s=2.0).retry_after_s == 2.0
