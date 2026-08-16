"""Anthropic/Claude wire family (anthropic SDK) — P1.5.

Canonical → Anthropic Messages API mapping:
- first system message → the `system` parameter;
- tool results → user-role `tool_result` content blocks; assistant tool calls →
  `tool_use` content blocks;
- structured output (P1.8): Anthropic has no response-schema parameter — the recorded
  pattern is a FORCED TOOL whose input schema is the requested schema; the tool_use
  input comes back as the structured JSON payload;
- `max_tokens` is mandatory on this wire — defaulted when the caller sets none.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
import anthropic

from ..errors import ModelError, classify_status
from ..types import ModelRequest, ModelResponse, StreamEvent, ToolCall, Usage
from .base import ProviderAdapter, status_of

_DEFAULT_MAX_TOKENS = 8192
_SCHEMA_TOOL = "emit_structured"

_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
           "tool_use": "tool_calls", "refusal": "content_filter"}


def _usage_of(u: Any) -> Usage:
    if u is None:
        return Usage()
    inp = getattr(u, "input_tokens", 0) or 0
    out = getattr(u, "output_tokens", 0) or 0
    return Usage(
        input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None),
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None),
    )


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"

    def _make_client(self) -> Any:
        kw: dict[str, Any] = {"api_key": self.api_key or "missing-key"}
        if self.base_url:
            kw["base_url"] = self.base_url
        return anthropic.AsyncAnthropic(**kw)

    def _to_error(self, e: Exception) -> ModelError:
        if isinstance(e, ModelError):
            return e
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return ModelError("timeout", str(e) or "request deadline exceeded",
                              provider=self.name)
        status = status_of(e)
        if status is not None:
            retry_after = None
            headers = getattr(getattr(e, "response", None), "headers", None)
            if headers is not None:
                try:
                    retry_after = float(headers.get("retry-after", "") or 0) or None
                except (TypeError, ValueError):
                    retry_after = None
            return classify_status(status, str(e), provider=self.name,
                                   retry_after_s=retry_after)
        return ModelError("unavailable", str(e), provider=self.name)

    # ── canonical → wire ─────────────────────────────────────────────────────────────────────

    def _payload(self, req: ModelRequest, model: str, stream: bool) -> dict[str, Any]:
        system = None
        messages: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "system" and system is None and not messages:
                system = m.content
                continue
            if m.role == "tool":
                messages.append({"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": m.tool_call_id,
                    "content": m.content}]})
            elif m.role == "assistant" and m.tool_calls:
                blocks: list[dict[str, Any]] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                blocks += [{"type": "tool_use", "id": tc.id, "name": tc.name,
                            "input": tc.arguments} for tc in m.tool_calls]
                messages.append({"role": "assistant", "content": blocks})
            else:
                messages.append({"role": m.role, "content": m.content})
        p = req.params
        payload: dict[str, Any] = {
            "model": model, "messages": messages,
            "max_tokens": int(p.get("max_tokens") or _DEFAULT_MAX_TOKENS),
        }
        if system:
            payload["system"] = system
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("stop", "stop_sequences")):
            if p.get(src) is not None:
                payload[dst] = p[src]
        tools = [{"name": t["name"], "description": t.get("description", ""),
                  "input_schema": t.get("input_schema")
                  or {"type": "object", "properties": {}}} for t in (req.tools or [])]
        if req.response_schema is not None:
            tools.append({"name": _SCHEMA_TOOL,
                          "description": "Emit the answer as structured data.",
                          "input_schema": req.response_schema})
            payload["tool_choice"] = {"type": "tool", "name": _SCHEMA_TOOL}
        if tools:
            payload["tools"] = tools
        if stream:
            payload["stream"] = True
        return payload

    # ── wire → canonical ─────────────────────────────────────────────────────────────────────

    def _map_response(self, resp: Any, model: str, structured: bool) -> ModelResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in getattr(resp, "content", None) or []:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                if structured and block.name == _SCHEMA_TOOL:
                    # The forced schema tool IS the structured answer, not a tool call.
                    text_parts.append(json.dumps(dict(block.input or {})))
                else:
                    tool_calls.append(ToolCall(id=block.id, name=block.name,
                                               arguments=dict(block.input or {})))
        finish = _FINISH.get(getattr(resp, "stop_reason", None) or "end_turn", "stop")
        if structured and finish == "tool_calls" and not tool_calls:
            finish = "stop"
        return ModelResponse(
            content="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else finish,
            usage=_usage_of(getattr(resp, "usage", None)),
            served_by={"provider": self.name, "model": model},
        )

    # ── contract methods ─────────────────────────────────────────────────────────────────────

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse:
        self._require_key()
        payload = self._payload(req, model, stream=False)
        try:
            resp = await asyncio.wait_for(
                self.client().messages.create(**payload), timeout=req.timeout_s)
        except Exception as e:  # noqa: BLE001
            raise self._to_error(e) from e
        return self._map_response(resp, model, structured=req.response_schema is not None)

    async def stream(self, req: ModelRequest, model: str) -> AsyncIterator[StreamEvent]:
        self._require_key()
        payload = self._payload(req, model, stream=True)
        usage = Usage()
        try:
            stream = await asyncio.wait_for(
                self.client().messages.create(**payload), timeout=req.timeout_s)
            async for event in stream:
                etype = getattr(event, "type", None)
                if etype == "message_start":
                    usage = _usage_of(getattr(getattr(event, "message", None),
                                              "usage", None))
                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    text = getattr(delta, "text", None)
                    if text:
                        yield StreamEvent(kind="text_delta", payload={"text": text})
                elif etype == "message_delta":
                    du = getattr(event, "usage", None)
                    if du is not None:
                        out = getattr(du, "output_tokens", 0) or 0
                        usage = Usage(input_tokens=usage.input_tokens, output_tokens=out,
                                      total_tokens=usage.input_tokens + out,
                                      cache_read_tokens=usage.cache_read_tokens,
                                      cache_write_tokens=usage.cache_write_tokens)
        except Exception as e:  # noqa: BLE001
            yield StreamEvent(kind="error", payload=self._to_error(e).payload())
            return
        yield StreamEvent(kind="usage", payload=usage.model_dump())
        yield StreamEvent(kind="served_by",
                          payload={"provider": self.name, "model": model})
        yield StreamEvent(kind="done")
