"""P1.2/P1.5 — provider adapter contract tests (recorded fixtures, no network, no keys).

Every adapter must satisfy the SAME canonical contract (05 §11): same request shape in,
same response/stream/error shapes out. Fixtures are recorded provider wire shapes
injected as fake SDK clients via the lazy `_client` seam — the SDKs themselves are not
required (the anthropic SDK cannot even install on this dev host; the api-test container
exercises real SDK imports).
"""

from __future__ import annotations

import json
from types import SimpleNamespace as NS

import pytest

from app.llm.adapters import for_provider, reset_adapter_cache
from app.llm.adapters.anthropic_ import AnthropicAdapter
from app.llm.adapters.google_ import GoogleAdapter
from app.llm.adapters.openai_compat import OpenAICompatAdapter
from app.llm.catalog import load as load_catalog
from app.llm.errors import ModelError
from app.llm.types import CanonicalMessage, ModelRequest, StreamEvent, ToolCall
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _req(**over) -> ModelRequest:
    base = dict(purpose="general",
                messages=[CanonicalMessage(role="system", content="be brief"),
                          CanonicalMessage(role="user", content="hi")])
    base.update(over)
    return ModelRequest(**base)


TOOLS = [{"name": "cloud.read", "description": "read a resource",
          "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}}]
SCHEMA = {"type": "object", "properties": {"intent": {"type": "string"}},
          "required": ["intent"]}


async def _drain(stream) -> list[StreamEvent]:
    return [ev async for ev in stream]


def _assert_termination(events: list[StreamEvent]) -> None:
    """The 05 §11 stream contract: exactly one done (after usage+served_by) XOR one error."""
    kinds = [e.kind for e in events]
    if "error" in kinds:
        assert kinds.count("error") == 1 and kinds[-1] == "error" and "done" not in kinds
    else:
        assert kinds[-3:] == ["usage", "served_by", "done"]
        assert kinds.count("done") == 1


class _Boom(Exception):
    def __init__(self, status: int, msg: str = "boom"):
        super().__init__(msg)
        self.status_code = status


# ── google ───────────────────────────────────────────────────────────────────────────────────

def _gg_resp(text="hello", fcalls=None, usage=(7, 3, 10)):
    return NS(text=text, function_calls=fcalls or [],
              usage_metadata=NS(prompt_token_count=usage[0],
                                candidates_token_count=usage[1],
                                total_token_count=usage[2],
                                cached_content_token_count=None))


def _google(resp=None, stream_chunks=None, exc=None) -> tuple[GoogleAdapter, dict]:
    seen: dict = {}

    async def generate_content(**kw):
        seen.update(kw)
        if exc:
            raise exc
        return resp

    async def generate_content_stream(**kw):
        seen.update(kw)
        if exc:
            raise exc
        async def chunks():
            for c in stream_chunks or []:
                yield c
        return chunks()

    async def embed_content(**kw):
        seen.update(kw)
        return NS(embeddings=[NS(values=[0.1] * 4)])

    a = GoogleAdapter("test-key")
    a._client = NS(aio=NS(models=NS(generate_content=generate_content,
                                    generate_content_stream=generate_content_stream,
                                    embed_content=embed_content)))
    return a, seen


async def test_google_generate_maps_canonical_shapes():
    a, seen = _google(resp=_gg_resp())
    r = await a.generate(_req(params={"temperature": 0.2, "max_tokens": 64}), "gemini-3.5-flash")
    assert seen["model"] == "gemini-3.5-flash"
    assert seen["contents"] == "hi"                       # single-turn maps to bare prompt
    assert seen["config"].system_instruction == "be brief"
    assert seen["config"].temperature == 0.2 and seen["config"].max_output_tokens == 64
    assert r.content == "hello" and r.finish_reason == "stop"
    assert r.usage.as_ledger() == {"input": 7, "output": 3, "total": 10}
    assert r.served_by.provider == "google" and r.served_by.model == "gemini-3.5-flash"


async def test_google_native_tools_and_tool_calls():
    a, seen = _google(resp=_gg_resp(text="", fcalls=[NS(id=None, name="cloud.read",
                                                        args={"id": "i-1"})]))
    r = await a.generate(_req(tools=TOOLS), "gemini-3.5-flash")
    decls = seen["config"].tools[0].function_declarations
    assert decls[0].name == "cloud.read"
    assert r.finish_reason == "tool_calls"
    assert r.tool_calls[0].name == "cloud.read" and r.tool_calls[0].arguments == {"id": "i-1"}
    assert r.tool_calls[0].args_hash  # identity for policy/idempotency (05 §11)


async def test_google_structured_output_config():
    a, seen = _google(resp=_gg_resp(text='{"intent":"read"}'))
    await a.generate(_req(response_schema=SCHEMA), "gemini-3.5-flash")
    assert seen["config"].response_mime_type == "application/json"
    assert seen["config"].response_schema == SCHEMA


async def test_google_stream_contract_and_error_stream():
    a, _ = _google(stream_chunks=[NS(text="he", usage_metadata=None),
                                  _gg_resp(text="llo", usage=(5, 2, 7))])
    events = await _drain(a.stream(_req(), "gemini-3.5-flash"))
    _assert_termination(events)
    assert "".join(e.payload["text"] for e in events if e.kind == "text_delta") == "hello"
    assert next(e for e in events if e.kind == "usage").payload["total_tokens"] == 7
    b, _ = _google(exc=_Boom(503))
    errs = await _drain(b.stream(_req(), "gemini-3.5-flash"))
    _assert_termination(errs)
    assert errs[-1].payload["kind"] == "unavailable"


async def test_google_error_mapping_and_unconfigured():
    a, _ = _google(exc=_Boom(429))
    with pytest.raises(ModelError) as e:
        await a.generate(_req(), "gemini-3.5-flash")
    assert e.value.kind == "upstream_rate_limited"
    with pytest.raises(ModelError) as e2:
        await GoogleAdapter("").generate(_req(), "gemini-3.5-flash")
    assert e2.value.kind == "auth_permanent"
    # Google's bad-key shape observed live: HTTP 400 + API_KEY_INVALID → auth_permanent.
    b, _ = _google(exc=_Boom(400, 'API key not valid: {"reason": "API_KEY_INVALID"}'))
    with pytest.raises(ModelError) as e3:
        await b.generate(_req(), "gemini-3.5-flash")
    assert e3.value.kind == "auth_permanent"


async def test_google_embeddings_dim_pin():
    a, seen = _google()
    vecs = await a.embed(["x"], "gemini-embedding-001", 768)
    assert seen["config"].output_dimensionality == 768
    assert len(vecs) == 1


# ── anthropic ────────────────────────────────────────────────────────────────────────────────

def _an_resp(blocks, stop="end_turn", usage=(11, 5)):
    return NS(content=blocks, stop_reason=stop,
              usage=NS(input_tokens=usage[0], output_tokens=usage[1],
                       cache_read_input_tokens=2, cache_creation_input_tokens=None))


def _anthropic(resp=None, stream_events=None, exc=None) -> tuple[AnthropicAdapter, dict]:
    seen: dict = {}

    async def create(**kw):
        seen.update(kw)
        if exc:
            raise exc
        if kw.get("stream"):
            async def events():
                for e in stream_events or []:
                    yield e
            return events()
        return resp

    a = AnthropicAdapter("test-key")
    a._client = NS(messages=NS(create=create))
    return a, seen


async def test_anthropic_generate_maps_canonical_shapes():
    a, seen = _anthropic(resp=_an_resp([NS(type="text", text="hello")]))
    r = await a.generate(_req(params={"max_tokens": 99}), "claude-sonnet-5")
    assert seen["model"] == "claude-sonnet-5" and seen["system"] == "be brief"
    assert seen["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["max_tokens"] == 99
    assert r.content == "hello" and r.usage.total_tokens == 16
    assert r.usage.cache_read_tokens == 2                  # 5-token-kind fidelity (04 §4.7)
    assert r.served_by.provider == "anthropic"


async def test_anthropic_max_tokens_defaulted_and_tools_mapping():
    a, seen = _anthropic(resp=_an_resp(
        [NS(type="tool_use", id="tu_1", name="cloud.read", input={"id": "i-1"})],
        stop="tool_use"))
    req = _req(tools=TOOLS,
               messages=[CanonicalMessage(role="user", content="read i-1"),
                         CanonicalMessage(role="assistant", content="",
                                          tool_calls=[ToolCall(id="tu_0", name="cloud.read",
                                                               arguments={"id": "i-0"})]),
                         CanonicalMessage(role="tool", content="ok", tool_call_id="tu_0")])
    r = await a.generate(req, "claude-sonnet-5")
    assert seen["max_tokens"] > 0                          # mandatory on this wire
    assert seen["tools"][0]["input_schema"]["properties"]["id"]["type"] == "string"
    assert seen["messages"][1]["content"][0]["type"] == "tool_use"
    assert seen["messages"][2]["content"][0]["type"] == "tool_result"
    assert r.finish_reason == "tool_calls" and r.tool_calls[0].id == "tu_1"


async def test_anthropic_structured_output_via_forced_tool():
    a, seen = _anthropic(resp=_an_resp(
        [NS(type="tool_use", id="tu_9", name="emit_structured",
            input={"intent": "read"})], stop="tool_use"))
    r = await a.generate(_req(response_schema=SCHEMA), "claude-sonnet-5")
    assert seen["tool_choice"] == {"type": "tool", "name": "emit_structured"}
    assert seen["tools"][-1]["input_schema"] == SCHEMA
    assert json.loads(r.content) == {"intent": "read"}     # schema tool IS the answer
    assert r.tool_calls == [] and r.finish_reason == "stop"


async def test_anthropic_stream_contract():
    a, _ = _anthropic(stream_events=[
        NS(type="message_start", message=NS(usage=NS(input_tokens=4, output_tokens=0,
                                                     cache_read_input_tokens=None,
                                                     cache_creation_input_tokens=None))),
        NS(type="content_block_delta", delta=NS(text="he")),
        NS(type="content_block_delta", delta=NS(text="llo")),
        NS(type="message_delta", usage=NS(output_tokens=6)),
    ])
    events = await _drain(a.stream(_req(), "claude-sonnet-5"))
    _assert_termination(events)
    u = next(e for e in events if e.kind == "usage").payload
    assert u["input_tokens"] == 4 and u["output_tokens"] == 6 and u["total_tokens"] == 10


async def test_anthropic_errors_and_unconfigured():
    a, _ = _anthropic(exc=_Boom(529, "overloaded"))
    with pytest.raises(ModelError) as e:
        await a.generate(_req(), "claude-sonnet-5")
    assert e.value.kind == "upstream_rate_limited"
    with pytest.raises(ModelError) as e2:
        await AnthropicAdapter("").generate(_req(), "claude-sonnet-5")
    assert e2.value.kind == "auth_permanent"


# ── openai_compat ────────────────────────────────────────────────────────────────────────────

def _oa_resp(content="hello", tool_calls=None, finish="stop", usage=(9, 4, 13)):
    return NS(choices=[NS(message=NS(content=content, tool_calls=tool_calls),
                          finish_reason=finish)],
              usage=NS(prompt_tokens=usage[0], completion_tokens=usage[1],
                       total_tokens=usage[2], prompt_tokens_details=None))


def _openai(resp=None, stream_chunks=None, exc=None) -> tuple[OpenAICompatAdapter, dict]:
    seen: dict = {}

    async def create(**kw):
        seen.update(kw)
        if exc:
            raise exc
        if kw.get("stream"):
            async def chunks():
                for c in stream_chunks or []:
                    yield c
            return chunks()
        return resp

    a = OpenAICompatAdapter("test-key", base_url="https://openrouter.ai/api/v1")
    a._client = NS(chat=NS(completions=NS(create=create)))
    return a, seen


async def test_openai_generate_and_json_string_tool_args():
    a, seen = _openai(resp=_oa_resp(content=None, finish="tool_calls", tool_calls=[
        NS(id="call_1", function=NS(name="cloud.read", arguments='{"id": "i-1"}'))]))
    r = await a.generate(_req(tools=TOOLS), "openrouter/auto")
    assert seen["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["tools"][0]["function"]["name"] == "cloud.read"
    assert r.tool_calls[0].arguments == {"id": "i-1"}      # JSON string → dict
    assert r.finish_reason == "tool_calls"
    assert r.usage.total_tokens == 13


async def test_openai_unparseable_tool_args_degrade_to_empty():
    a, _ = _openai(resp=_oa_resp(content=None, finish="tool_calls", tool_calls=[
        NS(id="c1", function=NS(name="cloud.read", arguments="{not json"))]))
    r = await a.generate(_req(tools=TOOLS), "gpt-4o-mini")
    assert r.tool_calls[0].arguments == {}


async def test_openai_structured_output_response_format():
    a, seen = _openai(resp=_oa_resp(content='{"intent":"read"}'))
    await a.generate(_req(response_schema=SCHEMA), "gpt-4o-mini")
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA


async def test_openai_stream_contract_include_usage():
    a, seen = _openai(stream_chunks=[
        NS(choices=[NS(delta=NS(content="he"))], usage=None),
        NS(choices=[NS(delta=NS(content="llo"))], usage=None),
        NS(choices=[], usage=NS(prompt_tokens=3, completion_tokens=2, total_tokens=5,
                                prompt_tokens_details=None)),
    ])
    events = await _drain(a.stream(_req(), "gpt-4o-mini"))
    assert seen["stream_options"] == {"include_usage": True}
    _assert_termination(events)
    assert next(e for e in events if e.kind == "usage").payload["total_tokens"] == 5


async def test_openai_errors():
    a, _ = _openai(exc=_Boom(401, "invalid key"))
    with pytest.raises(ModelError) as e:
        await a.generate(_req(), "gpt-4o-mini")
    assert e.value.kind == "auth_permanent"


# ── dispatch (data-driven construction) ─────────────────────────────────────────────────────

def test_for_provider_is_config_driven():
    reset_adapter_cache()
    cat = load_catalog()
    s = Settings(gemini_api_key="g", anthropic_api_key="a", openrouter_api_key="or")
    assert isinstance(for_provider("google", cat, s), GoogleAdapter)
    assert isinstance(for_provider("anthropic", cat, s), AnthropicAdapter)
    orr = for_provider("openrouter", cat, s)                # zero-code provider (§4 proof)
    assert isinstance(orr, OpenAICompatAdapter)
    assert orr.base_url == "https://openrouter.ai/api/v1" and orr.api_key == "or"
    assert for_provider("google", cat, s) is for_provider("google", cat, s)  # cached
    with pytest.raises(ModelError):
        for_provider("telepathy", cat, s)
