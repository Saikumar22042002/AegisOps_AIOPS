"""Canonical model-invocation contracts (Redesign/05 §11 — normative; 04 §4 semantics).

Provider-neutral by construction: nothing in this module may mention a provider, an SDK
type, or a wire format. Adapters translate these shapes per wire family. Evolution is
additive-only (C-03: no wire-version field exists yet — renames/retypes are breaking
changes and forbidden without a versioning decision).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── purposes (04 §4.3) — the ONLY model coupling an agent declares ────────────────────────────

PURPOSES: tuple[str, ...] = (
    "router", "extract", "planner", "loop.main", "inv_loop", "sre.triage", "critic",
    "retrieval_gate", "consolidation", "knowledge", "general", "judge", "embeddings",
)

# Governed purposes are never user-pinnable (04 §4.4): a per-run model choice must not
# change how classification/planning/judging behaves out from under the eval gate.
GOVERNED_PURPOSES: frozenset[str] = frozenset({"router", "planner", "loop.main", "judge"})

Role = Literal["system", "user", "assistant", "tool"]


def args_hash(arguments: dict[str, Any]) -> str:
    """Canonical-JSON SHA-256 of tool-call arguments — the identity used by policy
    binding, idempotency, and IP-1's repetition detector (05 §11)."""
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class ToolCall(BaseModel):
    """A model-issued call of a registered tool (05 §11)."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    args_hash: str = ""

    @model_validator(mode="after")
    def _fill_hash(self) -> ToolCall:
        if not self.args_hash:
            object.__setattr__(self, "args_hash", args_hash(self.arguments))
        return self


class ToolResult(BaseModel):
    """What re-enters the model as the tool-role message. Middleware wraps the same data
    into the run-log ToolObservation (05 §3) — same facts, two audiences."""
    model_config = ConfigDict(extra="forbid")

    tool_call_id: str = Field(min_length=1)
    ok: bool
    content: str | dict[str, Any] = ""
    error: dict[str, str] | None = None  # {"kind": ..., "message": ...}
    stage: str | None = None             # which middleware stage failed (05 §3)

    @model_validator(mode="after")
    def _error_iff_failed(self) -> ToolResult:
        if not self.ok and self.error is None:
            raise ValueError("a failed ToolResult must carry error={kind, message}")
        return self


class CanonicalMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] | None = None   # assistant role only
    tool_call_id: str | None = None            # tool role only

    @model_validator(mode="after")
    def _role_shape(self) -> CanonicalMessage:
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls are only valid on assistant messages")
        if self.tool_call_id and self.role != "tool":
            raise ValueError("tool_call_id is only valid on tool messages")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool messages must reference the tool_call_id they answer")
        return self


class RoutePlan(BaseModel):
    """Resolved routing for one request (04 §4.4): a primary binding plus a validated
    fallback chain. Resolution order: per-request pin (run creation only, non-governed
    purposes) → model_bindings row → models.yaml default."""
    model_config = ConfigDict(extra="forbid")

    purpose: str
    provider: str
    model: str
    fallbacks: list[tuple[str, str]] = Field(default_factory=list)  # [(provider, model)]
    params: dict[str, Any] = Field(default_factory=dict)
    pinned_by: Literal["request", "binding", "default"] = "default"

    @field_validator("purpose")
    @classmethod
    def _known_purpose(cls, v: str) -> str:
        if v not in PURPOSES:
            raise ValueError(f"unknown purpose {v!r}; known: {', '.join(PURPOSES)}")
        return v


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    messages: list[CanonicalMessage] = Field(min_length=1)
    tools: list[dict[str, Any]] | None = None   # ToolDef dicts (05 §1); adapters translate
    params: dict[str, Any] = Field(default_factory=dict)
    # response_schema: JSON Schema for structured output (P1.8) — adapters that support
    # native structured output enforce it; others fall back to fenced-JSON parsing.
    response_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)  # run_id/org_id/agent_kind/prompt_ref

    @field_validator("purpose")
    @classmethod
    def _known_purpose(cls, v: str) -> str:
        if v not in PURPOSES:
            raise ValueError(f"unknown purpose {v!r}; known: {', '.join(PURPOSES)}")
        return v

    @property
    def timeout_s(self) -> float:
        return float(self.params.get("timeout_s", 120.0))  # 07 P1.2 default


class Usage(BaseModel):
    """The five token kinds of 04 §4.7; rolls into `llm_usage` verbatim."""
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    def as_ledger(self) -> dict[str, int]:
        """The dict shape `usage_ledger.record_usage(usage=…)` accepts today."""
        return {"input": self.input_tokens, "output": self.output_tokens,
                "total": self.total_tokens}


class ServedBy(BaseModel):
    """Honest serving metadata on every response (04 §4.6; badge per 10-V)."""
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    requested_model: str | None = None
    fallback_hop: int = 0


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: Literal["stop", "length", "tool_calls", "content_filter"] = "stop"
    usage: Usage = Field(default_factory=Usage)
    served_by: ServedBy
    latency_ms: int = 0


class StreamEvent(BaseModel):
    """One event of a model stream (05 §11). Every stream terminates with exactly one
    `done` (after `usage` + `served_by`) or exactly one `error` carrying a ModelError
    payload — adapters enforce this shape, the service relies on it."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["text_delta", "tool_call_delta", "usage", "served_by", "error", "done"]
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload_shape(self) -> StreamEvent:
        need = {"text_delta": ("text",), "error": ("kind", "message")}
        for key in need.get(self.kind, ()):
            if key not in self.payload:
                raise ValueError(f"{self.kind} events require payload[{key!r}]")
        return self
