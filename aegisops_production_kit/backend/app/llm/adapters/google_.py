"""Google/Gemini wire family (google-genai SDK) — P1.2.

Behavior carried over verbatim from the transitional `integrations/gemini.py` singleton:
async client, `system_instruction` config, usage from `usage_metadata` (totals arrive on
the final stream chunk), embeddings with a pinned output dimensionality. New per the
architecture: canonical message mapping, typed errors, request timeout (default 120s,
07 P1.2), generation params, native structured output + function calling (P1.8).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from ..errors import ModelError, classify_status
from ..types import ModelRequest, ModelResponse, StreamEvent, ToolCall, Usage
from .base import ProviderAdapter, status_of


def _usage_of(resp: Any) -> Usage:
    md = getattr(resp, "usage_metadata", None)
    if md is None:
        return Usage()
    return Usage(
        input_tokens=getattr(md, "prompt_token_count", 0) or 0,
        output_tokens=getattr(md, "candidates_token_count", 0) or 0,
        total_tokens=getattr(md, "total_token_count", 0) or 0,
        cache_read_tokens=getattr(md, "cached_content_token_count", None),
    )


class GoogleAdapter(ProviderAdapter):
    name = "google"

    def _make_client(self) -> Any:
        from google import genai
        # Client construction does not call the network; calls do (gemini.py contract).
        return genai.Client(api_key=self.api_key or "missing-key")

    def _to_error(self, e: Exception) -> ModelError:
        if isinstance(e, ModelError):
            return e
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return ModelError("timeout", str(e) or "request deadline exceeded",
                              provider=self.name)
        # Google reports a bad/revoked key as HTTP 400 API_KEY_INVALID (observed live,
        # 2026-08-10) — that is an auth_permanent condition (open the breaker), not a
        # malformed request.
        if "API_KEY_INVALID" in str(e):
            return ModelError("auth_permanent", str(e), provider=self.name)
        status = status_of(e)
        if status is not None:
            return classify_status(status, str(e), provider=self.name)
        return ModelError("unavailable", str(e), provider=self.name)

    # ── canonical → wire ─────────────────────────────────────────────────────────────────────

    def _split(self, req: ModelRequest) -> tuple[str | None, Any]:
        """System instruction + contents. The single [system?, user] shape every current
        caller sends maps to a bare prompt string; richer histories map per-role."""
        from google.genai import types as gtypes
        system = None
        rest = []
        for m in req.messages:
            if m.role == "system" and system is None and not rest:
                system = m.content
                continue
            rest.append(m)
        if len(rest) == 1 and rest[0].role == "user" and not rest[0].tool_calls:
            return system, rest[0].content
        contents: list[Any] = []
        for m in rest:
            if m.role == "tool":
                contents.append(gtypes.Content(role="user", parts=[
                    gtypes.Part.from_function_response(
                        name=m.tool_call_id or "tool", response={"result": m.content})]))
            elif m.role == "assistant":
                parts: list[Any] = []
                if m.content:
                    parts.append(gtypes.Part(text=m.content))
                for tc in m.tool_calls or []:
                    parts.append(gtypes.Part.from_function_call(name=tc.name,
                                                                args=tc.arguments))
                contents.append(gtypes.Content(role="model", parts=parts))
            else:
                contents.append(gtypes.Content(role="user",
                                               parts=[gtypes.Part(text=m.content)]))
        return system, contents

    def _config(self, req: ModelRequest, system: str | None) -> Any:
        from google.genai import types as gtypes
        p = req.params
        tools = None
        if req.tools:
            decls = [gtypes.FunctionDeclaration(
                name=t["name"], description=t.get("description", ""),
                parameters=t.get("input_schema") or {"type": "object", "properties": {}},
            ) for t in req.tools]
            tools = [gtypes.Tool(function_declarations=decls)]
        return gtypes.GenerateContentConfig(
            system_instruction=system or None,
            tools=tools,
            temperature=p.get("temperature"),
            top_p=p.get("top_p"),
            max_output_tokens=p.get("max_tokens"),
            stop_sequences=p.get("stop"),
            # P1.8 structured output: schema-enforced JSON at the provider.
            response_mime_type="application/json" if req.response_schema else None,
            response_schema=req.response_schema,
        )

    # ── wire → canonical ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _tool_calls(resp: Any) -> list[ToolCall]:
        out: list[ToolCall] = []
        for i, fc in enumerate(getattr(resp, "function_calls", None) or []):
            out.append(ToolCall(id=getattr(fc, "id", None) or f"call_{i}",
                                name=fc.name, arguments=dict(fc.args or {})))
        return out

    # ── contract methods ─────────────────────────────────────────────────────────────────────

    async def generate(self, req: ModelRequest, model: str) -> ModelResponse:
        self._require_key()
        system, contents = self._split(req)
        try:
            resp = await asyncio.wait_for(
                self.client().aio.models.generate_content(
                    model=model, contents=contents, config=self._config(req, system)),
                timeout=req.timeout_s)
        except Exception as e:  # noqa: BLE001 — mapped to the taxonomy
            raise self._to_error(e) from e
        tool_calls = self._tool_calls(resp)
        return ModelResponse(
            content=resp.text or "",
            tool_calls=tool_calls,
            finish_reason="tool_calls" if tool_calls else "stop",
            usage=_usage_of(resp),
            served_by={"provider": self.name, "model": model},
        )

    async def stream(self, req: ModelRequest, model: str) -> AsyncIterator[StreamEvent]:
        self._require_key()
        system, contents = self._split(req)
        usage = Usage()
        try:
            stream = await asyncio.wait_for(
                self.client().aio.models.generate_content_stream(
                    model=model, contents=contents, config=self._config(req, system)),
                timeout=req.timeout_s)
            async for chunk in stream:
                u = _usage_of(chunk)
                if u.total_tokens:
                    usage = u  # totals arrive on the final chunk (gemini.py contract)
                if getattr(chunk, "text", None):
                    yield StreamEvent(kind="text_delta", payload={"text": chunk.text})
        except Exception as e:  # noqa: BLE001
            yield StreamEvent(kind="error", payload=self._to_error(e).payload())
            return
        yield StreamEvent(kind="usage", payload=usage.model_dump())
        yield StreamEvent(kind="served_by",
                          payload={"provider": self.name, "model": model})
        yield StreamEvent(kind="done")

    async def embed(self, texts: list[str], model: str, dim: int) -> list[list[float]]:
        self._require_key()
        from google.genai import types as gtypes
        try:
            resp = await self.client().aio.models.embed_content(
                model=model, contents=texts,
                config=gtypes.EmbedContentConfig(output_dimensionality=dim))
        except Exception as e:  # noqa: BLE001
            raise self._to_error(e) from e
        return [list(e.values) for e in resp.embeddings]
