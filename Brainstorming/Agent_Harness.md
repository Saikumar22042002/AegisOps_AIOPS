# Agent Harness — Provider-Agnostic Runtime + LLM Provider Layer

> Blueprint for the layer between AegisOps agents and every LLM. After this is built,
> **no agent imports an LLM SDK, no agent knows which model serves it, and switching
> models is a UI action, not a code change.** This document is implementation-ready:
> types, protocols, module layout, migration map, tests.
>
> Companions: `CloudOps_Harness.md` (governed execution), `Proposed_Architecture.md`
> (where these layers sit), `Implementation_Roadmap.md` (sequencing).

---

## 1. Design goals and non-negotiables

**Goals**

| # | Goal | Acceptance test |
|---|------|-----------------|
| G1 | Provider-agnostic agents | `grep -r "google.genai\|anthropic\|openai" backend/app/agents/` returns zero hits |
| G2 | UI model switching, zero agent change | Bind `purpose=planner → claude-sonnet-5` in admin UI; next run uses it; no deploy |
| G3 | Capability-aware routing | A purpose that requires native tool-calling can never be bound to a model without it |
| G4 | Honest serving | Every response records the model that *actually* answered (post-fallback), surfaced in UI + Langfuse + ledger |
| G5 | Resilience without lies | Retry/fallback never silently degrades a governed decision; budget breaches halt honestly |
| G6 | One streaming dialect | UI/gateways consume one normalized event stream regardless of provider |
| G7 | Cost is a first-class record | Append-only `llm_usage` ledger survives Langfuse outages; budgets can stop a run |

**Non-negotiables carried over from the current system (do not weaken):**

- The LLM never authors HCL, never selects unapproved code, never calls a mutating tool
  directly. Mutation exits only through the governed pipeline (`CloudOps_Harness.md`).
- Approval interrupts stay durable (Postgres-checkpointed) and resumable cross-process.
- Tenancy (`security/tenancy.py`), RBAC (`security/rbac.py`), redaction
  (`security/redaction.py`) wrap every new surface, including this one.
- Honesty rules from the STAB series: no surface may claim what didn't happen. A
  fallback answer is labeled with the model that served it.

**Explicit non-goals**

- Not a general chat proxy for other teams (build for AegisOps' purposes, not a platform product).
- No hot-reloadable "skills" that steer agents outside governance (rejected in the waku
  gap analysis for good reason — an mtime-reloaded prompt is an unreviewed change to
  platform behavior).
- No provider-specific features leaking upward (if only one provider can do X, X lives
  behind a capability flag, or it doesn't ship).

---

## 2. The shape in one diagram

```
agents (router, cloudops, sre, knowledge, general, inv-loop, planner, judge)
   │        only import: app.llm.service  +  app.harness.*
   ▼
┌─────────────────────────── app/llm (Provider Layer) ───────────────────────────┐
│ service.generate(purpose=…)                                                    │
│   → ModelRouter.resolve(purpose, org, needs)  → RoutePlan(primary, fallbacks)  │
│   → ResilientExecutor(retry, circuit-breaker, fallback, budget check)          │
│       → ProviderAdapter.complete/stream (anthropic | openai-compat | google |  │
│         bedrock | azure | vertex | litellm-escape-hatch)                       │
│   → UsageLedger.append (org, run, purpose, model, tokens, latency, outcome)    │
│   ← ChatResponse{parts, stop_reason, usage, served_by}                         │
└─────────────────────────────────────────────────────────────────────────────────┘
   ▲                                   ▲
   │ ModelCatalog + CapabilityRegistry │ config: models.yaml (code-reviewed)
   │                                   │ overlay: model_bindings table (UI-editable, org-scoped)
```

Two layers, one contract:

- **`app/harness`** — the agent kernel: the loop, the tool registry, budgets, events,
  subagents, interrupts. Knows nothing about providers.
- **`app/llm`** — the provider layer: canonical types, catalog, router, adapters,
  resilience, streaming, usage. Knows nothing about agents.

The only coupling between an agent and a model is a **purpose string**.

---

## 3. Canonical model (`app/llm/types.py`)

One dialect internally, translated at the adapter boundary — waku proves the whole
Anthropic↔OpenAI bridge is ~110 lines (`waku/loop/models.py:197-311`); we standardize
on a **neutral** shape instead of either vendor's, because enterprise adds Bedrock/
Vertex/Azure whose "native" shapes differ again.

```python
# app/llm/types.py — dataclasses only; no SDK imports allowed in this module
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]

@dataclass(frozen=True)
class TextPart:
    text: str

@dataclass(frozen=True)
class ThinkingPart:
    # Normalized reasoning content. `signature` carries provider-opaque proof
    # (Anthropic signed thinking, Gemini thought_signature) that MUST be echoed
    # back on the next turn of a tool loop or the follow-up call 400s.
    # waku learned this the hard way (models.py:286-294) — we keep the lesson.
    text: str = ""
    signature: str | None = None
    redacted: bool = False

@dataclass(frozen=True)
class ToolCallPart:
    id: str
    name: str
    args: dict[str, Any]
    # Provider-opaque baggage that must round-trip (e.g. Gemini extra_content).
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ToolResultPart:
    call_id: str
    content: str                      # tool observations are strings (harness rule)
    is_error: bool = False

Part = TextPart | ThinkingPart | ToolCallPart | ToolResultPart

@dataclass(frozen=True)
class Message:
    role: Role
    parts: tuple[Part, ...]

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]      # JSON Schema — the one true tool shape

@dataclass(frozen=True)
class ReasoningSpec:
    effort: Literal["off", "low", "medium", "high"] = "off"
    budget_tokens: int | None = None  # explicit override; adapters map per provider

@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    tool_choice: Literal["auto", "required", "none"] | str = "auto"  # str = named tool
    max_tokens: int = 8192
    temperature: float | None = None
    reasoning: ReasoningSpec = ReasoningSpec()
    response_schema: dict[str, Any] | None = None   # structured output when set
    stop_sequences: tuple[str, ...] = ()
    meta: RequestMeta = field(default_factory=lambda: RequestMeta())

@dataclass(frozen=True)
class RequestMeta:
    org_id: str | None = None
    run_id: str | None = None
    purpose: str = "general"
    trace_id: str | None = None       # == run_id today; keep that invariant

@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

@dataclass(frozen=True)
class ServedBy:
    provider_id: str                  # "anthropic"
    model_id: str                     # "claude-sonnet-5" — the ACTUAL server
    requested_model_id: str           # what the router first chose
    fallback_hop: int = 0             # 0 = primary; >0 = which fallback served
    attempts: int = 1

@dataclass(frozen=True)
class ChatResponse:
    parts: tuple[Part, ...]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens",
                         "content_filter", "refusal", "error"]
    usage: Usage
    served_by: ServedBy
    raw_response_id: str | None = None

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.parts if isinstance(p, ToolCallPart)]
```

**Streaming events** — one normalized union; adapters assemble provider deltas into
these (the OpenAI tool-arg slot-assembly trick from `waku/loop/models.py:345-352`
lives inside the adapter, invisible above it):

```python
@dataclass(frozen=True)
class TextDelta:        text: str
@dataclass(frozen=True)
class ThinkingDelta:    text: str
@dataclass(frozen=True)
class ToolCallStart:    id: str; name: str
@dataclass(frozen=True)
class ToolCallArgsDelta: id: str; args_json_delta: str
@dataclass(frozen=True)
class ToolCallEnd:      call: ToolCallPart          # parsed, validated args
@dataclass(frozen=True)
class UsageUpdate:      usage: Usage
@dataclass(frozen=True)
class StreamDone:       response: ChatResponse       # the fully-assembled final
@dataclass(frozen=True)
class StreamFault:      error: LLMError; recoverable: bool

StreamEvent = TextDelta | ThinkingDelta | ToolCallStart | ToolCallArgsDelta \
            | ToolCallEnd | UsageUpdate | StreamDone | StreamFault
```

Rules that make streaming sane:

1. **Every stream ends with exactly one `StreamDone` or `StreamFault`.** Consumers
   never assemble state themselves; `StreamDone.response` is authoritative.
2. Mid-stream transport failure with partial text → executor may retry non-streaming
   and emit a single synthetic `StreamDone` (waku's fallback at `loop/agent.py:77-79`),
   but only if no `ToolCallEnd` was already emitted — replaying tool calls is how you
   double-execute. If tool calls were emitted: `StreamFault(recoverable=False)`.
3. `ThinkingDelta` feeds the UI's live "reasoning" affordance only; persistence of
   raw thinking is forbidden (existing confidentiality rule — the Analysis tab shows
   a privacy-safe summary, never chain-of-thought).

---

## 4. Provider Layer (`app/llm/`)

### 4.1 Module layout

```
backend/app/llm/
  types.py            # §3 — zero dependencies
  errors.py           # canonical error taxonomy (§4.8)
  catalog.py          # ModelCatalog + CapabilityRegistry (§4.3)
  router.py           # ModelRouter + RoutePlan (§4.4)
  service.py          # generate()/stream() — the ONLY agent-facing entry (§6)
  executor.py         # ResilientExecutor: retry/breaker/fallback/budget (§4.8)
  usage.py            # UsageLedger + budgets (§4.9)
  reasoning.py        # ReasoningSpec ↔ provider mapping table (§4.5)
  emulation.py        # prompted-tools / prompted-JSON fallbacks (§4.6)
  adapters/
    base.py           # ProviderAdapter protocol + shared helpers
    anthropic_.py     # native Messages API (also: MiniMax/Kimi/GLM endpoints)
    openai_compat.py  # OpenAI + any base_url-compatible (OpenRouter/DeepSeek/xAI/…)
    google_.py        # native google-genai (Gemini; thought_signature handling)
    bedrock_.py       # AWS Bedrock Converse (enterprise residency)
    azure_openai_.py  # Azure OpenAI (enterprise residency)
    litellm_.py       # OPTIONAL escape hatch for the long tail (§4.11)
  config/
    models.yaml       # the catalog source of truth (code-reviewed)
```

### 4.2 ProviderAdapter protocol

```python
# app/llm/adapters/base.py
class ProviderAdapter(Protocol):
    id: str                                    # "anthropic" | "openai-compat" | …

    async def complete(self, req: ChatRequest, model: ResolvedModel) -> ChatResponse: ...
    def stream(self, req: ChatRequest, model: ResolvedModel) -> AsyncIterator[StreamEvent]: ...
    async def list_models(self) -> list[dict]: # live catalog for the admin UI
    async def health(self) -> AdapterHealth:   # cheap auth/reachability probe
```

Adapter implementation rules:

- **Adapters are the only files that import provider SDKs.** Enforced by lint (§6).
- Adapters are stateless translators + one owned HTTP client; all policy (retry,
  fallback, budget) lives in the executor so behavior is uniform.
- All quirks live in catalog metadata, not `if model.startswith(...)` (waku's
  `max_completion_tokens` fallback at `models.py:255-270` becomes quirk
  `param_name_max_tokens: "max_completion_tokens"`; Gemini's echo requirement becomes
  `requires_thought_signature_echo: true` handled in `google_.py`/`openai_compat.py`).
- Sync SDKs (google-genai constructor today) get thread-offloaded **inside** the
  adapter — the "no blocking I/O on the event loop" fix lands here once, for every
  caller (this was FIX.md B6; the boto3-in-coroutine class of bug).

Wire families (why these six cover everything):

| Family | Serves | Notes |
|---|---|---|
| anthropic | Anthropic API; Kimi/GLM/MiniMax Anthropic-compatible endpoints | waku runs 4 vendors through this one shape |
| openai-compat | OpenAI, OpenRouter, DeepSeek, xAI, Groq, Ollama/vLLM, LM Studio | one adapter + `base_url` = the entire self-hosted story |
| google | Gemini via native `google-genai` | native (not the OpenAI-compat shim) because Vertex auth, safety settings, and thought signatures are first-class |
| bedrock | Claude/Llama/Mistral inside a customer's AWS boundary | Converse API; SigV4 auth; the enterprise residency answer |
| azure-openai | GPT inside a customer's Azure boundary | deployment-name indirection |
| litellm (optional) | anything else | §4.11 |

### 4.3 ModelCatalog + CapabilityRegistry

`models.yaml` is the reviewed source of truth; a DB overlay adds org-scoped bindings
and admin-registered self-hosted endpoints. **Capabilities are facts, tiers are
policy, quirks are adapter hints** — three different lifecycles, one file:

```yaml
# app/llm/config/models.yaml
providers:
  anthropic:    {adapter: anthropic,     key_env: ANTHROPIC_API_KEY}
  openai:       {adapter: openai-compat, key_env: OPENAI_API_KEY}
  google:       {adapter: google,        key_env: GEMINI_API_KEY}
  bedrock:      {adapter: bedrock,       auth: aws_default_chain, region_env: AWS_REGION}
  openrouter:   {adapter: openai-compat, key_env: OPENROUTER_API_KEY,
                 base_url: https://openrouter.ai/api/v1}
  local_vllm:   {adapter: openai-compat, key_env: null,
                 base_url: ${VLLM_BASE_URL}}          # air-gapped deployments

models:
  claude-sonnet-5:
    provider: anthropic
    context_window: 200000
    max_output: 64000
    capabilities: [tools, parallel_tools, streaming, reasoning,
                   structured_output, vision, prompt_cache]
    cost_per_mtok: {input: 3.00, output: 15.00, cache_read: 0.30}
    tiers: [balanced, flagship]
  claude-haiku-4-5:
    provider: anthropic
    capabilities: [tools, parallel_tools, streaming, structured_output]
    cost_per_mtok: {input: 1.00, output: 5.00}
    tiers: [fast, gate]
  gemini-3.5-flash:
    provider: google
    context_window: 1048576
    capabilities: [tools, streaming, reasoning, structured_output, vision]
    quirks: [thought_signature_echo]
    cost_per_mtok: {input: 0.30, output: 2.50}
    tiers: [fast, balanced]
  gpt-5.3-chat-latest:
    provider: openai
    capabilities: [tools, streaming, structured_output, vision]
    quirks: [max_completion_tokens_param]
    tiers: [balanced]
  # waku's field lesson, encoded so routing can never repeat it: the gpt-5.6
  # reasoning line 400s on function tools via chat.completions — that model id
  # simply does not list `tools`, and G3 makes the bad binding unrepresentable.

purposes:                       # defaults; DB overlay may rebind per org
  router:            {model: gemini-3.5-flash,  reasoning: off,   needs: [structured_output]}
  cloudops.extract:  {model: gemini-3.5-flash,  reasoning: off,   needs: [structured_output]}
  planner:           {model: claude-sonnet-5,   reasoning: high,  needs: [tools, structured_output]}
  inv_loop:          {model: claude-sonnet-5,   reasoning: medium, needs: [tools]}
  sre.triage:        {model: gemini-3.5-flash,  reasoning: medium, needs: [tools]}
  knowledge:         {model: gemini-3.5-flash,  needs: [streaming]}
  general:           {model: gemini-3.5-flash,  needs: [streaming]}
  retrieval_gate:    {model: claude-haiku-4-5,  max_tokens: 600}
  consolidation:     {model: claude-haiku-4-5,  max_tokens: 600}
  judge:             {model: claude-sonnet-5,   needs: [structured_output]}
  embeddings:        {model: gemini-embedding-001}  # rides the same registry — but note:
                     # pgvector columns are pinned at 768 dims (db/models.py EMBED_DIM);
                     # rebinding embeddings is a re-embedding MIGRATION, not a dropdown
                     # change. The admin UI must say so and refuse a hot swap.

fallbacks:                      # capability-compatible chains, checked at load time
  claude-sonnet-5:   [claude-haiku-4-5, gemini-3.5-flash]
  gemini-3.5-flash:  [gemini-3.1-flash-lite, claude-haiku-4-5]
```

```python
# app/llm/catalog.py
class CapabilityRegistry:
    def can(self, model_id: str, cap: str) -> bool: ...
    def assert_compatible(self, model_id: str, needs: list[str]) -> None:
        """Raise IncompatibleBinding at CONFIG TIME, not at request time.
        G3: a purpose needing `tools` cannot be bound to a model without them —
        the admin UI greys the option out, and a hand-edited yaml fails boot."""
    def quirk(self, model_id: str, name: str) -> Any | None: ...
```

Load-time validation (boot fails loudly, another STAB-honesty carryover):
every purpose's `needs` ⊆ its bound model's capabilities; every fallback chain member
satisfies the same needs as the head; every provider's key env var is present *or*
the provider is marked disabled and every purpose bound to it has a viable fallback.

### 4.4 ModelRouter

```python
# app/llm/router.py
@dataclass(frozen=True)
class Needs:
    capabilities: tuple[str, ...] = ()
    min_context: int = 0
    max_cost_tier: str | None = None      # policy ceiling, e.g. org says "no flagship"
    residency: str | None = None          # "aws-only" orgs route to bedrock models

@dataclass(frozen=True)
class RoutePlan:
    primary: ResolvedModel                 # model + provider + params + quirks
    fallbacks: tuple[ResolvedModel, ...]   # pre-validated capability-compatible
    params: GenParams                      # merged: purpose defaults ⊕ binding ⊕ call

class ModelRouter:
    def resolve(self, purpose: str, org: OrgCtx, needs: Needs | None = None) -> RoutePlan:
        """Resolution order (first hit wins), all org-scoped:
        1. per-request pin      — only for purposes policy marks user-pinnable
                                  (general/knowledge chat; NEVER governed purposes)
        2. org binding          — model_bindings row (UI-set, audited, eval-gated)
        3. models.yaml purpose default
        Then: capability check (hard), residency filter (hard),
        cost-tier ceiling (hard), attach fallback chain (filtered by same rules)."""
```

Two routing decisions worth calling out because they encode governance:

- **Governed purposes are not user-pinnable.** The July finding — a model menu that
  silently did nothing — gets fixed in both directions: the menu becomes real *and*
  scoped. A user picking "Claude" in chat affects `general`/`knowledge` replies. The
  models used for `router`, `planner`, `cloudops.*`, `judge` change only through the
  org-admin binding flow (RBAC: platform_admin), because those choices alter what
  infrastructure gets planned — that's a change-management event, with an audit row
  and an eval gate (§4.10), not a dropdown whim.
- **Sticky-by-run:** all calls within one run reuse the RoutePlan resolved at run
  start (recorded on the run row). Mid-run rebinding would make an approved plan and
  its execution the product of different brains; it also preserves prompt-cache
  affinity.

### 4.5 Reasoning Engine (`app/llm/reasoning.py`)

One `ReasoningSpec` normalized across vendors — a pure mapping table, unit-testable:

| `effort` | Anthropic | OpenAI | Gemini (native) |
|---|---|---|---|
| off | thinking disabled | `reasoning_effort: "minimal"`/omit | `thinking_budget: 0`* |
| low | `budget_tokens: 2048` | `reasoning_effort: "low"` | `thinking_budget: 2048` |
| medium | `budget_tokens: 8192` | `"medium"` | `thinking_budget: 8192` |
| high | `budget_tokens: 24576` | `"high"` | `thinking_budget: 24576` |

*Catalog quirk `min_thinking` covers models that cannot fully disable thinking.
`budget_tokens` set explicitly overrides the effort table. Two hard rules:

1. **`max_tokens` must include thinking headroom.** waku raised its cap from 2048 →
   8192 after watching reasoning models hit `stop_reason=max_tokens` mid-thought and
   return an *empty* reply (`waku/config.py:63-68`). The executor enforces
   `max_tokens ≥ reasoning_budget + 1024` and rejects mis-sized requests at build time.
2. **Signature round-tripping is the adapter's job.** `ThinkingPart.signature` and
   `ToolCallPart.extra` are opaque bytes upstairs; adapters re-attach them on the next
   call. No agent ever sees them.

### 4.6 Tool-calling translation

Canonical `ToolSpec` (JSON Schema) → provider shape inside each adapter (Anthropic
`tools=[{name, description, input_schema}]`; OpenAI `function` wrapper; Gemini
`FunctionDeclaration`; Bedrock `toolSpec`). Parallel calls normalize to multiple
`ToolCallPart`s in one response; `tool_choice` maps per vendor.

**Emulation tiers** (capability-gated, from the CapabilityRegistry):

| Missing capability | Emulation | Allowed for |
|---|---|---|
| `tools` | Hermes-style prompted tool-calling: schemas in system prompt, model emits `<tool_call>{json}</tool_call>`, parser + one repair round-trip | **read-only purposes only** (`inv_loop`, `sre.triage`). Governed purposes hard-require native `tools` — a guessed parse is not an audit-grade record of intent. |
| `structured_output` | prompted JSON + `jsonschema` validation + one repair attempt, then honest failure | any purpose; router/extract already parse JSON today, this makes it principled |
| `streaming` | buffered pseudo-stream (single `StreamDone`) | any purpose |

This is how self-hosted Llama/Mistral behind vLLM joins the fleet without weakening
the governed path: they serve chat/knowledge immediately, and only graduate to
tool-driving purposes if their endpoint does native function calling.

### 4.7 Streaming normalization

Adapter → `StreamEvent` union (§3). The harness bridges it onto the existing run
event bus (`agents/events.py` Emitter → Redis Streams → SSE/gateways):

- `TextDelta` → existing `token` SSE event (UI contract unchanged — zero frontend rework)
- `ToolCallStart/End` → `step`-style "calling list_deployments…" affordances
- `ThinkingDelta` → live reasoning ticker (never persisted)
- `StreamDone.usage` → ledger append

The dedupe-by-monotonic-id and tail-from-now replay semantics already in the Redis
bus stay exactly as they are; the provider layer produces events, it does not own
delivery.

### 4.8 Resilience (`app/llm/executor.py`)

Canonical error taxonomy (`app/llm/errors.py`) — every adapter maps into it:

```
rate_limited | overloaded | timeout | network | server_error        → RETRIABLE
auth | invalid_request | model_not_found | quota_exhausted          → NOT RETRIABLE
content_filtered | refusal                                          → NOT RETRIABLE (surface honestly)
context_overflow                                                    → NOT RETRIABLE HERE (harness compacts & re-asks)
```

Executor policy, in order:

1. **Retry** retriables: max 3 attempts, exponential backoff + full jitter, honor
   `Retry-After`. Never retry a response that arrived but won't parse — that's a
   repair round-trip, not a retry (waku's judge encodes exactly this split:
   `waku/ops/judge.py` retries only the API call, never a parsed-but-bad reply).
2. **Circuit breaker** per (provider, model): open after 5 failures/60s window;
   half-open probe; while open, RoutePlans skip straight to fallbacks. Breaker state
   lives in Redis so all workers share one view of provider health.
3. **Fallback** down the RoutePlan chain on exhausted retries/open breaker.
   Every hop is recorded (`ServedBy.fallback_hop`) and emitted as a visible run event
   — "answered by claude-haiku-4-5 (fallback: gemini overloaded)". **Exception:** the
   `planner` and `judge` purposes never silently fall back — a planning-quality
   downgrade mid-run is a governance event; the run pauses and reports, the user
   chooses (the existing provider_errors retry-with-fix card is the right UI).
4. **Budget gate** before every call: per-run cost ceiling and per-org daily ceiling
   from `usage.py`. Breach → `BudgetExceeded` → harness halts at the next safe
   boundary with the honest-partial report (never mid-apply; see CloudOps_Harness).
5. **Timeout**: per-call default 120s (matching waku's hung-call rule), per-purpose
   override (planner gets more, gate gets 15s).

Note the split with `agents/provider_errors.py`: that module classifies **cloud**
-provider failures (6 kinds: credentials_expired, api_disabled, iam_denied,
name_taken, quota_exceeded, bad_location) out of Terraform/SDK error text — it stays,
unchanged, on the execution side. The LLM taxonomy above is its sibling for the
reasoning side, and reuses the same *presentation* pattern (title/cause/next-step
card + one-click retry). While in that file: fix the audit-found dead branch —
`classify_provider_error` emits `kind="bad_location"` but `suggest_retry` matches
`"bad_region"`, so the one-click region retry is currently unreachable for every
real classified failure.

### 4.9 Usage ledger + budgets (`app/llm/usage.py`)

Append-only Postgres table (this was BOTH-WEAK gap #2 — neither waku nor AegisOps
can *stop* a run on spend):

```sql
CREATE TABLE llm_usage (
  id            BIGSERIAL PRIMARY KEY,
  ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
  org_id        UUID NOT NULL,
  run_id        UUID,            -- NULL for non-run calls (consolidation sweeps)
  purpose       TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,   -- the SERVING model (post-fallback)
  requested_model TEXT NOT NULL,
  input_tokens  INT, output_tokens INT, reasoning_tokens INT,
  cache_read_tokens INT, cache_write_tokens INT,
  latency_ms    INT,
  outcome       TEXT NOT NULL,   -- ok | error:<kind> | fallback:<n>
  agent_kind    TEXT NOT NULL DEFAULT 'main'   -- main | subagent | gate | judge
);
```

- **Tokens are ground truth; dollars are derived at read time** from the catalog's
  price table (waku's `usage.jsonl` rationale, made multi-tenant and SQL-queryable).
- Sub-agent and gate spend land in the same ledger with `agent_kind` set — waku's
  regression (uncounted delegated tokens silently understating every score,
  `waku/tools/experimental.py:19-22`) becomes structurally impossible.
- Budgets: `settings.max_cost_per_run_usd`, `org.daily_budget_usd` (nullable = off).
  Checked in the executor (per call) and at harness iteration boundaries (per loop).
- Langfuse keeps per-generation cost for trace UX; the ledger is the billing/
  chargeback record that survives key rotation, retention purges, and outages.

### 4.10 Configuration & the UI switching flow

Three layers, three change-speeds, three audiences:

| Layer | Who changes it | Cadence | Audit |
|---|---|---|---|
| `models.yaml` | platform engineers, via PR | model releases | git history + CI eval gate |
| `model_bindings` DB table | org admin, via Settings UI | weekly-ish | row: who/when/why + before/after |
| per-request pin | end user, chat model picker | per message | run row records it; only unpinnable purposes |

```sql
CREATE TABLE model_bindings (
  org_id     UUID NOT NULL,
  purpose    TEXT NOT NULL,
  model_id   TEXT NOT NULL,
  params     JSONB NOT NULL DEFAULT '{}',   -- temperature/max_tokens/reasoning overrides
  eval_state TEXT NOT NULL DEFAULT 'pending',  -- pending | passed | failed | waived
  updated_by TEXT NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
  reason     TEXT,
  PRIMARY KEY (org_id, purpose)
);
```

Admin flow (this is what "switching LLMs from the UI with minimal configuration"
concretely means):

1. Settings → Models lists providers with key status (live `health()` probe) and each
   provider's **live catalog** (`list_models()` — waku's Settings-picker pattern), not
   a hardcoded menu that can lie.
2. Admin binds `purpose → model`. The UI greys out incompatible models (G3, computed
   from CapabilityRegistry — not vibes).
3. **Test before live:** the binding runs the offline eval smoke for that purpose
   (router dataset cases for `router`, plan-fidelity cases for `planner`; see
   Roadmap Phase 0). Green → `eval_state=passed` → live for the next run. Red → the
   binding stays staged with the failing cases shown. `waived` exists for break-glass,
   requires a reason, and is loud in the audit log.
4. Rollback = rebind previous model (the audit row has it) — one click.

No restart, no deploy, no agent change. Boot-time env (`GEMINI_MODEL`) stops being
the selection mechanism and becomes only the default seed for `models.yaml`.

### 4.11 Build vs LiteLLM (decision box)

**Decision: own the six thin adapters; offer `litellm_.py` as an optional,
off-by-default escape hatch for the long tail.**

- For: waku demonstrates 11 providers over 2 wire formats in ~300 lines total — the
  translation is small, and owning it means: deterministic streaming/tool semantics,
  our error taxonomy (not somebody's), no dependency churn on the hottest path in the
  product, capability metadata co-designed with the router, and quirks handled where
  we can test them.
- Against building: LiteLLM tracks hundreds of vendors' drift for free.
- Resolution: the adapter protocol makes this a non-exclusive choice — LiteLLM *is
  just another adapter*. Enterprise deployments (Bedrock/Azure/vLLM) are covered by
  owned adapters; a customer's exotic vendor rides the escape hatch without a fork,
  and graduates to an owned adapter when it earns hot-path traffic.

---

## 5. The Agent Kernel (`app/harness/`)

The kernel is deliberately small — pi's lesson (a competitive harness core is ~100
lines: a loop, tools, context) and waku's proof (`loop/agent.py` is 114 lines). The
enterprise weight goes into *policies around* the loop, never into the loop.

### 5.1 AgentSpec + the loop

```python
# app/harness/spec.py
@dataclass(frozen=True)
class Budgets:
    max_iterations: int = 6
    max_tool_calls: int = 8            # inherits investigation.MAX_CALLS today
    max_cost_usd: float | None = None  # per agent-run slice of the run budget
    wall_clock_s: int = 300

@dataclass(frozen=True)
class AgentSpec:
    name: str                          # "investigator", "planner", "general"
    purpose: str                       # → ModelRouter binding; the ONLY model coupling
    system_prompt: PromptRef           # versioned prompt registry entry (§5.3)
    tools: ToolPolicy                  # READ_ONLY_FROZEN | GOVERNED_PROPOSE | NONE
    budgets: Budgets = Budgets()
    context_recipe: str = "default"    # ContextEngine recipe name
```

```python
# app/harness/kernel.py — the loop; structure follows waku/loop/agent.py:63-113,
# with four enterprise insertions marked ①-④
async def run_agent(spec: AgentSpec, goal: str, ctx: RunCtx) -> AgentResult:
    registry = ctx.tools.for_policy(spec.tools)          # frozen before the loop
    transcript = []
    for iteration in range(1, spec.budgets.max_iterations + 1):
        ctx.budget.check(spec)                           # ① cost/wall-clock gate
        messages = ctx.context.assemble(spec, goal, transcript)  # ② per-ITERATION
        response = await llm.generate(                   #   reassembly, not per-node
            purpose=spec.purpose, messages=messages,
            tools=registry.specs(), meta=ctx.meta, stream=ctx.stream)
        transcript.append(assistant_turn(response))
        ctx.emit("agent_llm", iteration=iteration, served_by=response.served_by,
                 usage=response.usage)
        if not response.tool_calls:
            return AgentResult(reply=response.text, iterations=iteration,
                               transcript=transcript, stopped="answered")
        results = []
        for call in response.tool_calls:
            obs = await registry.execute(call, ctx)      # ③ NEVER raises upward:
            results.append(tool_result(call, obs))       #   errors are observations
            ctx.emit("agent_tool", tool=call.name, ok=not obs.is_error)
        transcript.append(user_tool_results(results))
        await ctx.checkpoint(spec, iteration, transcript)  # ④ resumable loops
    return AgentResult(reply=partial_report(transcript),  # honest partial, never a lie
                       iterations=spec.budgets.max_iterations, stopped="budget")
```

- ① Budgets: iteration cap (waku), call cap (investigation registry), **cost cap
  (new — the gap neither system closed)**, wall clock. All breaches exit through the
  honest-partial path (`exec_loop._partial_outcome` precedent: report what was done,
  what wasn't, why).
- ② Context is reassembled every iteration by the existing `build_context` machinery
  — its per-purpose budgets and 70/30 recent/digest split are already better than
  waku's flat window; the fix is *cadence* (today it runs once per graph node).
- ③ waku's single most important line, kept verbatim as policy: a failed tool call
  becomes the observation string `Error running X: …` — the model retries or reroutes;
  the loop never crashes (`waku/tools/registry.py:57-58`).
- ④ Each iteration checkpoint makes *loops* resumable, not just DAG nodes — the same
  reconciler that resumes stranded runs can resume a stranded investigation.

Stuck-detection (OpenHands lesson): the kernel tracks `(tool, args-hash)` pairs; the
third identical call in a row injects a system observation "you already ran this —
change approach or conclude," and the fifth ends the run through the budget path.

### 5.2 ToolRegistry v2

```python
# app/harness/tools.py
@dataclass(frozen=True)
class ToolDef:
    spec: ToolSpec                       # model-facing (name/desc/schema)
    fn: Callable[..., Awaitable[str]]
    effect: Literal["read", "propose", "governed_mutation"]
    timeout_s: int = 30
    idempotent: bool = True
    rate_key: str | None = None          # per-org rate bucket
    redact: bool = True                  # output passes redaction before the model
```

Three policy classes — this is the split-trust boundary expressed as registry types:

| Policy | Contents | Who runs it |
|---|---|---|
| `READ_ONLY_FROZEN` | describe/list/get across clouds, Prometheus, world-model `impact_of`, RAG search, `get_turn` recall | inv-loop, SRE triage, discovery. Inherits `investigation.py`'s registration-time mutation-name denylist + freeze + shared budget — that mechanism is already right. |
| `GOVERNED_PROPOSE` | `propose_goal_dag`, `estimate_cost`, `policy_precheck` — tools that *draft* artifacts for the governed pipeline | planner. Output is data for human approval, never execution. |
| `GOVERNED_MUTATION` | exactly one tool: `execute_governed_step(...)` whose interior is the deterministic pipeline | **no LLM-facing registry ever contains it.** The execution plane calls it; agents cannot. This is the Split-Trust boundary from the target-architecture doc, unchanged. |

Middleware chain around every execution (order matters):
`tenancy_scope → rbac_check → rate_limit → timeout → execute → redact → audit → observe`.
Failures at any stage return observation strings (③ above); the audit stage writes
the `audit_log` row that today only two call sites write.

MCP servers mount as `read`-effect tools only, behind an org-admin allowlist —
waku's MCP bridge pattern (`waku/tools/mcp_client.py`) with governance: a
mutation-shaped MCP tool name gets refused at registration by the same denylist.

### 5.3 Prompts as versioned artifacts

`PromptRef("router.classify", "v7")` — prompts live in a registry table with content
hash, owner, and changelog. Every `llm_usage` row and Langfuse generation records the
prompt version. This closes BOTH-WEAK gap #3 (neither system can answer "which prompt
revision caused this regression?") and is what makes the eval gate (Roadmap Phase 0)
attributable.

### 5.4 Events

Keep the existing `Emitter` + Redis Streams bus — the waku gap analysis scored it
*more* capable than waku's observer/compose seam, and the GW-1 gateways already
consume it. The kernel adds three event kinds: `agent_llm` (iteration, served_by,
usage), `agent_tool` (name, ok, ms), `agent_gate` (retrieval-gate decisions), so the
flow diagram can animate loop iterations the way waku's dashboard animates its stages.

### 5.5 Subagents

`spawn(child_spec, subgoal)` with: **shared budget pool** (child draws down the
parent's `Budgets` — the `investigation.spawn` call-budget-sharing pattern,
extended to cost), isolated context (child sees its subgoal + a curated slice, not
the parent transcript — Claude Code's context-isolation rationale), typed result
(`AgentResult`, not prose parsing), events relayed with `agent=` tags (waku's
delegate-to-pi relay), and ledger rows with `agent_kind='subagent'`. Depth cap: 1.
Subagents are read/propose only — mutation never delegates.

### 5.6 Interrupts

`NeedsApproval(payload)` raised by any `propose`-effect flow → the harness
checkpoints and surfaces the approval artifact; resume re-enters at the loop
iteration boundary. Implementation stays LangGraph's durable interrupt over the
Postgres checkpointer — it is the one mechanism the gap analysis flagged as
irreplaceable (a different process can resume it after a restart). The kernel wraps
it so *agents* don't import LangGraph either — graph machinery becomes an
implementation detail of the harness, which is what makes a later Temporal migration
(Roadmap decision gate) a harness-internal swap.

---

## 6. The zero-agent-change contract

What an agent file may import:

```python
from app.llm.service import generate, stream          # purpose-keyed LLM access
from app.llm.types import Message, TextPart, ...       # canonical types
from app.harness import run_agent, AgentSpec, ToolDef  # the kernel
```

What an agent file may never import (CI-enforced via import-linter contract +
ruff `flake8-tidy-imports` banned-api):

```
google.genai, anthropic, openai, boto3.bedrock*, litellm   # SDKs → adapters only
app.llm.adapters.*                                          # below the service line
langgraph.*                                                  # harness-internal (§5.6)
```

The service facade (the *entire* API agents see):

```python
# app/llm/service.py
async def generate(*, purpose: str, messages: Sequence[Message],
                   tools: Sequence[ToolSpec] = (), response_schema: dict | None = None,
                   meta: RequestMeta, **overrides) -> ChatResponse: ...

def stream(*, purpose: str, messages: Sequence[Message],
           tools: Sequence[ToolSpec] = (), meta: RequestMeta,
           **overrides) -> AsyncIterator[StreamEvent]: ...
```

Convenience wrappers `classify_json(...)` and `answer(...)` keep the current
`agents/llm.py` call-shape so the migration is mechanical (§7).

---

## 7. Migration map (strangler, not rewrite)

Current state (audited 2026-08-03 at commit `a974290`): the U3 commit made model
selection *real but thin*. `POST /chat` reads `body.model`, validates it against
`llm/registry.get_provider` (unknown → 400), and binds it per-run via the
`set_run_model` contextvar (`api/chat.py:258-261, 330`; task-isolated, tested). The
menu honestly lists the 3 servable Gemini ids. **But the seam validates without
dispatching**: the resolved provider object is discarded at the call site
(`_provider, resolved_model = get_provider(...)`), `GeminiProvider.agenerate/astream/
aembed` have zero callers, and every real inference call goes through the
`get_gemini()` module singleton (`agents/llm.py:39,49,97`). Adding a second provider
today means rewriting `agents/llm.py`, not registering a class. Also true today:
**zero native tool-calling** (the `tools=` param reaches the Gemini config at
`gemini.py:103` and nobody populates it — structured behavior is prompt-and-parse
JSON), **no per-call timeout**, **no generation params** (no temperature or
max_output_tokens anywhere in `backend/`), **no per-purpose models** (router and
planner share one contextvar), **no cost ledger** (tokens live only in Langfuse;
embeddings aren't recorded even there), and the frontend menu is a hardcoded literal
that never fetches `GET /models` — sync enforced by a comment.

| Step | Change | Blast radius |
|---|---|---|
| 1 | Create `app/llm/{types,errors,catalog}.py` + `adapters/google_.py` wrapping today's Gemini client (thread-offloaded, typed errors) | none — new code |
| 2 | Reimplement `agents/llm.py`'s public functions on `app/llm/service` with `purpose=` threaded from each caller (`router.py:120`, `cloudops.py:44/67/198`, `devops.py:61`, `general.py:69`, `knowledge.py:53`, `sre.py:121`); keep `classify_json`/`stream_answer` signatures byte-compatible; delete the caller-less `generate()` helper | all agent nodes, mechanically |
| 3 | Add `anthropic_.py` + `openai_compat.py`, models.yaml, router, executor | none until bound |
| 4 | `model_bindings` table + Settings UI + eval-gated promotion; frontend menu switches from its hardcoded literal to fetching `GET /models` (the endpoint already exists at `api/integrations.py:114-122`; today nothing calls it) | frontend drift fix |
| 5 | Move retry/error mapping out of `gemini.py` into executor; `provider_errors.py` consumes typed errors | error paths |
| 6 | `llm_usage` ledger + budget gates | new table |
| 7 | Kernel: extract the INV loop (new) on `investigation.py`'s frozen registry; SRE `_collect_telemetry` and `cloudops._read_path` become its first two callers | read paths only |
| 8 | Retire `integrations/gemini.py` singleton; `integrations/llm/` becomes a re-export shim, then deleted | cleanup |

Each step ships alone, behind the eval gate. Governed-path behavior is unchanged
through step 8 — the approval interrupt, plan guard, and Terraform pipeline never
notice the brain transplant. **Sequencing rule from the gap analysis, kept:** the
eval gate (Roadmap Phase 0) lands *before* step 3–4, because swapping providers
without a behavioral gate is how a silent quality regression ships.

---

## 8. Testing strategy

| Layer | Test | Mechanism |
|---|---|---|
| Adapters | golden request/response contract tests per adapter; streaming assembly (text, parallel tool-args interleave, usage-only final chunk); quirk behaviors (max_completion_tokens fallback fires only on that error) | recorded fixtures, no network |
| Taxonomy | every provider error class maps to exactly one canonical kind | table-driven |
| Router | binding resolution order; capability incompatibility raises at config load; residency filtering; fallback chains preserve `needs` | pure unit |
| Executor | retry/backoff/Retry-After honoring; breaker open/half-open; fallback hop recording; budget halt at boundary; no-retry-after-ToolCallEnd rule | fake adapter with fault injection |
| Kernel | failed-tool-as-observation; stuck detector; checkpoint/resume mid-loop; budget partial-report shape; subagent budget draw-down | scripted fake client (waku's injectable-client seam — `Waku(client=…)` — is the precedent) |
| Behavior | the Phase-0 eval dataset per purpose + LLM-judge thresholds + CI release gate | offline, recorded transcripts |
| Live smoke | one $0.01 canary per configured provider on boot/binding change: auth, tools, streaming each verified | opt-in, admin-triggered |

---

## 9. Honesty rules (system-wide, enforced here)

1. Every response carries `served_by`; UI badges the actual model per message
   (waku's `meta.model` per-card honesty, upgraded with fallback provenance).
2. Fallback hops are visible run events, never silent.
3. Planner/judge never auto-fallback (§4.8.3) — quality downgrades on governed
   purposes are human decisions.
4. Budget breaches produce the honest-partial report, never a truncated answer
   dressed as a complete one.
5. The model menu renders the live catalog + real bindings; nothing selectable is
   fictional. (The July audit's finding #9 — a four-model menu wired to nothing — is
   the anti-pattern this entire layer exists to delete.)
6. `content_filtered`/`refusal` surface as themselves, not as generic errors.
