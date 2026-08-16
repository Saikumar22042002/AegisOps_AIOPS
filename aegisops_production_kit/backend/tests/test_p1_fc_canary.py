"""P1.8 — opt-in live canaries (07 P1.8: "native FC verified per adapter with an
opt-in $0.01 canary").

NEVER runs in CI or routine suites: each test needs BOTH the explicit opt-in env
(`AEGISOPS_FC_CANARY=1`) AND real provider credentials. One tiny prompt per configured
adapter proves, against the live API: native tool calling round-trips the canonical
ToolCall contract, and structured output honors a response schema.
"""

from __future__ import annotations

import os

import pytest

from app.llm import catalog as catalog_mod
from app.llm.adapters import for_provider
from app.llm.types import CanonicalMessage, ModelRequest
from app.settings import get_settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


_CANARY_MODELS = {"google": "gemini-3.5-flash", "anthropic": "claude-haiku-4-5-20251001",
                  "openai_compat": "gpt-4o-mini"}

TOOL = [{"name": "get_weather", "description": "Current weather for a city.",
         "input_schema": {"type": "object",
                          "properties": {"city": {"type": "string"}},
                          "required": ["city"]}}]
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"]}


def _skip_unless_opted(provider: str) -> tuple:
    if os.getenv("AEGISOPS_FC_CANARY") != "1":
        pytest.skip("live canary: set AEGISOPS_FC_CANARY=1 to spend ~$0.01")
    settings = get_settings()
    cat = catalog_mod.load()
    if not cat.provider_configured(provider, settings):
        pytest.skip(f"{provider}: no credentials configured")
    return settings, cat


@pytest.mark.parametrize("provider", ["google", "anthropic", "openai_compat"])
async def test_native_tool_call_round_trip(provider):
    settings, cat = _skip_unless_opted(provider)
    adapter = for_provider(provider, cat, settings)
    req = ModelRequest(
        purpose="general", tools=TOOL, params={"max_tokens": 200},
        messages=[CanonicalMessage(role="user",
                                   content="What's the weather in Paris? Use the tool.")])
    resp = await adapter.generate(req, _CANARY_MODELS[provider])
    assert resp.finish_reason == "tool_calls", f"{provider} did not call the tool"
    call = resp.tool_calls[0]
    assert call.name == "get_weather"
    assert "paris" in str(call.arguments.get("city", "")).lower()
    assert call.args_hash and resp.usage.total_tokens > 0


@pytest.mark.parametrize("provider", ["google", "anthropic", "openai_compat"])
async def test_structured_output_honors_schema(provider):
    import json
    settings, cat = _skip_unless_opted(provider)
    adapter = for_provider(provider, cat, settings)
    req = ModelRequest(
        purpose="general", response_schema=SCHEMA, params={"max_tokens": 200},
        messages=[CanonicalMessage(role="user", content="Say hi, structured.")])
    resp = await adapter.generate(req, _CANARY_MODELS[provider])
    parsed = json.loads(resp.content)
    assert isinstance(parsed.get("answer"), str) and parsed["answer"]
