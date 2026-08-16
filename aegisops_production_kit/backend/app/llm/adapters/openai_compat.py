"""OpenAI-compatible wire family (openai SDK) — P1.5.

Serves ANY endpoint speaking the Chat Completions protocol: OpenAI itself, OpenRouter
(a distinct catalog provider riding this family with its own base_url + key), local
gateways, etc. `base_url=None` = the SDK default.

Canonical → wire mapping notes:
- tool arguments travel as JSON STRINGS on this wire — encoded on send, parsed on
  receive (unparseable arguments become {} rather than crashing the run);
- structured output (P1.8): `response_format: json_schema` (strict);
- streaming usage requires `stream_options: {include_usage: true}` — the final chunk
  carries totals.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from ..errors import ModelError, classify_status
from ..types import ModelRequest, ModelResponse, StreamEvent, ToolCall, Usage
from .base import ProviderAdapter, status_of

_FINISH = {"stop": "stop", "length": "length", "tool_calls": "tool_calls",
           "content_filter": "content_filter", "function_call": "tool_calls"}


def _usage_of(u: Any) -> Usage:
    if u is None:
        return Usage()
    inp = getattr(u, "prompt_tokens", 0) or 0
    out = getattr(u, "completion_tokens", 0) or 0
    details = getattr(u, "prompt_tokens_details", None)
    return Usage(
        input_tokens=inp, output_tokens=out,
        total_tokens=getattr(u, "total_tokens", 0) or (inp + out),
        cache_read_tokens=getattr(details, "cached_tokens", None) if details else None,
    )


def _parse_args(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class OpenAICompatAdapter(ProviderAdapter):
    name = "openai_compat"

    def _make_client(self) -> Any:
        import openai
        kw: dict[str, Any] = {"api_key": self.api_key or "missing-key"}
        if self.base_url:
            kw["base_url"] = self.base_url
        return openai.AsyncOpenAI(**kw)

    def _to_error(self, e: Exception) -> ModelError:
        if isinstance(e, ModelError):
            return e
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return ModelError("timeout", str(e) or "request deadline exceeded",
                              provider=self.name)
        status = status_of(e)
        if status is not None:
            return classify_status(status, str(e), provider=self.name)
        return ModelError("unavailable", str(e), provider=self.name)

    # ── canonical → wire ─────────────────────────────────────────────────────────────────────

    def _payload(self, req: ModelRequest, model: str, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        for m in req.messages:
            if m.role == "tool":
                messages.append({"role": "tool", "tool_call_id": m.tool_call_id,
                                 "content": m.content if isinstance(m.content, str)
                                 else json.dumps(m.content)})
            elif m.role == "assistant" and m.tool_calls:
                entry: dict[str, Any] = {"role": "assistant",
                                         "content": m.content or None}
                entry["tool_calls"] = [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.name,
                                 "arguments": json.dumps(tc.arguments)},
                } for tc in m.tool_calls]
                messages.append(entry)
            else:
                messages.append({"role": m.role, "content": m.content})
        p = req.params
        payload: dict[str, Any] = {"model": model, "messages": messages}
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                         ("max_tokens", "max_tokens"), ("stop", "stop")):
            if p.get(src) is not None:
                payload[dst] = p[src]
        if req.tools:
            payload["tools"] = [{
                "type": "function",
                "function": {"name": t["name"],
                             "description": t.get("description", ""),
                             "parameters": t.get("input_schema")
                             or {"type": "object", "properties": {}}},
            } for t in req.tools]
        if req.response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_answer", "strict": True,
                                "schema": req.response_schema},
            }
        if stream:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        return payload

    # ── wire → canonical ─────────────────────────────────────────────────────────────────────

    def _map_response(self, resp: Any, model: str) -> ModelResponse:
        choice = (getattr(resp, "choices", None) or [None])[0]
        if choice is None:
            raise ModelError("unavailable", "provider returned no choices",
                             provider=self.name)
        msg = choice.message
        tool_calls = [ToolCall(id=tc.id, name=tc.function.name,
                               arguments=_parse_args(tc.function.arguments))
                      for tc in (getattr(msg, "tool_calls", None) or [])]
        finish = _FINISH.get(getattr(choice, "finish_reason", None) or "stop", "stop")
        return ModelResponse(
            content=getattr(msg, "content", None) or "",
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
                self.client().chat.completions.create(**payload), timeout=req.timeout_s)
        except Exception as e:  # noqa: BLE001
            raise self._to_error(e) from e
        return self._map_response(resp, model)

    async def stream(self, req: ModelRequest, model: str) -> AsyncIterator[StreamEvent]:
        self._require_key()
        payload = self._payload(req, model, stream=True)
        usage = Usage()
        try:
            stream = await asyncio.wait_for(
                self.client().chat.completions.create(**payload), timeout=req.timeout_s)
            async for chunk in stream:
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = _usage_of(u)  # totals arrive on the final chunk
                choice = (getattr(chunk, "choices", None) or [None])[0]
                delta = getattr(choice, "delta", None) if choice else None
                text = getattr(delta, "content", None) if delta else None
                if text:
                    yield StreamEvent(kind="text_delta", payload={"text": text})
        except Exception as e:  # noqa: BLE001
            yield StreamEvent(kind="error", payload=self._to_error(e).payload())
            return
        yield StreamEvent(kind="usage", payload=usage.model_dump())
        yield StreamEvent(kind="served_by",
                          payload={"provider": self.name, "model": model})
        yield StreamEvent(kind="done")
