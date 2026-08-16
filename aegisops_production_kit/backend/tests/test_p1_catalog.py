"""P1.4 — model catalog / capability registry pins (Redesign/04 §4.3-4.5, 07 P1.4).

The catalog is data: these tests pin the VALIDATION rules (an invalid yaml must refuse
boot) and the data-driven provider mechanics (a new provider is yaml + adapter, never a
code branch — proven by OpenRouter riding the openai_compat wire family).
"""

from __future__ import annotations

import textwrap

import pytest

from app.llm import catalog
from app.llm.errors import ModelError
from app.llm.types import GOVERNED_PURPOSES, PURPOSES
from app.settings import Settings


def _settings(**over) -> Settings:
    return Settings(**over)


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "models.yaml"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


MINIMAL_OK = """
providers:
  google: {wire_family: google, settings_field: gemini_api_key}
models:
  - id: m1
    provider: google
    capabilities: [streaming, structured_output, tools_native]
  - id: m2
    provider: google
    capabilities: [streaming]
  - id: e1
    provider: google
    capabilities: [embeddings]
    embedding_dim: 768
purposes:
  router:         {model: m1, fallbacks: none, requires: [structured_output]}
  planner:        {model: m1, fallbacks: none}
  loop.main:      {model: m1, fallbacks: none}
  judge:          {model: m1, fallbacks: none}
  extract:        {model: m1, fallbacks: [m2], requires: [structured_output]}
  inv_loop:       {model: m1, fallbacks: [m2]}
  sre.triage:     {model: m1, fallbacks: [m2]}
  critic:         {model: m1, fallbacks: [m2]}
  retrieval_gate: {model: m1, fallbacks: [m2]}
  consolidation:  {model: m1, fallbacks: [m2]}
  knowledge:      {model: m1, fallbacks: [m2]}
  general:        {model: m1, fallbacks: [m2]}
  embeddings:     {model: e1, fallbacks: none, requires: [embeddings]}
"""


def test_shipped_yaml_is_valid_and_covers_every_purpose():
    cat = catalog.load()
    assert set(cat.purposes) == set(PURPOSES)
    for p in GOVERNED_PURPOSES:  # never silent-fallback
        assert cat.purposes[p].fallbacks == ()
    assert cat.models["gemini-embedding-001"].embedding_dim == 768  # ADR-02 pin


def test_provider_configuration_is_settings_field_driven():
    cat = catalog.load()
    on = _settings(gemini_api_key="k")
    off = _settings(gemini_api_key="")
    assert cat.provider_configured("google", on) is True
    assert cat.provider_configured("google", off) is False
    ids = {m.id for m in cat.configured_models(on)}
    assert "gemini-3.5-flash" in ids and "claude-sonnet-5" not in ids
    both = _settings(gemini_api_key="k", anthropic_api_key="a")
    assert "claude-sonnet-5" in {m.id for m in cat.configured_models(both)}


def test_openrouter_is_pure_configuration_over_openai_compat():
    """The §4 extensibility proof: OpenRouter exists with ZERO adapter code."""
    cat = catalog.load()
    family, key, base = cat.transport("openrouter", _settings(openrouter_api_key="or-k"))
    assert family == "openai_compat"
    assert key == "or-k"
    assert base == "https://openrouter.ai/api/v1"
    # openai_compat itself resolves base_url from Settings (empty = SDK default).
    family2, _, base2 = cat.transport(
        "openai_compat", _settings(openai_api_key="x", openai_base_url="http://local:1234/v1"))
    assert family2 == "openai_compat" and base2 == "http://local:1234/v1"
    _, _, base3 = cat.transport("openai_compat", _settings(openai_api_key="x"))
    assert base3 is None


def test_capability_gating_selectable():
    cat = catalog.load()
    with pytest.raises(ModelError):  # embeddings model can't serve router
        cat.selectable("gemini-embedding-001", "router")
    assert cat.selectable("gemini-3.5-flash", "router").tools_any is True


def test_invalid_catalogs_refuse_boot(tmp_path):
    # missing purpose
    bad = MINIMAL_OK.replace("  general:        {model: m1, fallbacks: [m2]}\n", "")
    with pytest.raises(ModelError, match="purpose general"):
        catalog.load(_write(tmp_path, bad))
    # governed purpose with a fallback chain
    bad = MINIMAL_OK.replace("router:         {model: m1, fallbacks: none, requires: [structured_output]}",
                             "router:         {model: m1, fallbacks: [m2], requires: [structured_output]}")
    with pytest.raises(ModelError, match="never silent-fallback"):
        catalog.load(_write(tmp_path, bad))
    # fallback chain neither list nor explicit none (07 P1.4)
    bad = MINIMAL_OK.replace("general:        {model: m1, fallbacks: [m2]}",
                             "general:        {model: m1}")
    with pytest.raises(ModelError, match="explicit"):
        catalog.load(_write(tmp_path, bad))
    # default model lacking a required capability
    bad = MINIMAL_OK.replace("extract:        {model: m1, fallbacks: [m2], requires: [structured_output]}",
                             "extract:        {model: m2, fallbacks: [m1], requires: [structured_output]}")
    with pytest.raises(ModelError, match="lacks required capability"):
        catalog.load(_write(tmp_path, bad))
    # provider whose wire family has no adapter
    bad = MINIMAL_OK.replace("google: {wire_family: google, settings_field: gemini_api_key}",
                             "google: {wire_family: telepathy, settings_field: gemini_api_key}")
    with pytest.raises(ModelError, match="no adapter"):
        catalog.load(_write(tmp_path, bad))


def test_unknown_model_is_a_typed_error():
    with pytest.raises(ModelError) as e:
        catalog.load().model("gpt-o9-ultra")
    assert e.value.kind == "invalid_request"
