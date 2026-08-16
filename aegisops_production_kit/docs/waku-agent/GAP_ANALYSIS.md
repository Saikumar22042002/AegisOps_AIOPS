# waku-agent → AegisOps: Gap Analysis

**Analysis only. No code was changed to produce this document.**

## Scope and method

Two codebases were read in full at the source level. Every claim below cites `file:line`
from real code — never a README, never a design doc.

| | waku-agent | AegisOps |
|---|---|---|
| Path | `C:\Users\Sai kumar\Documents\Gen AI Projects\Agent-Harness\waku-agent` | this repo |
| Python LOC (app) | 7,663 (`waku/**.py`) | 15,640 (`backend/app/**.py`) |
| Core harness LOC | 2,838 (`waku/loop` + `waku/memory` + `waku/runtime` + `waku/tools`) | 4,097 (`backend/app/agents/**.py`) |
| Test/eval LOC | 3,915 (`evals/**.py`) | 12,852 (`backend/tests/**.py`) |
| Test functions | 208 deterministic + 2 judge | 596 across 93 files |
| Dashboard/UI | 2,535 LOC stdlib-served static JS/CSS/HTML (`waku/ops/static`) | 5,209 LOC Next.js/TS (`frontend`) |
| LLM providers | 11 provider entries (`waku/loop/models.py:54-109`) | 1 (Gemini) behind a registry seam (`backend/app/integrations/llm/registry.py`) |

The two systems are not the same kind of program. waku is a **single-user local
assistant harness**: one SQLite file, no auth, no tenancy, no infrastructure mutation.
AegisOps is a **multi-tenant governed infrastructure platform**: Postgres/Redis/Neo4j,
Keycloak OIDC, 8 RBAC roles, Terraform-only mutation behind a human-approval interrupt.
Each is judged against its own job in §2, and the requirements difference is stated
explicitly rather than scored as a defect.

---

## 1. What waku has that AegisOps doesn't

### 1.1 The iterative agent loop (the single biggest structural gap)

waku's loop is 114 lines and is a genuine `observe → reason → act → repeat` cycle.

**Iteration mechanics.** `waku/loop/agent.py:63` — `for iteration in range(1, max_iterations + 1):`.
Each pass makes one LLM call with the *current* message list (`agent.py:80-86`), appends the
assistant's content blocks to working memory (`agent.py:91`), executes every requested tool
(`agent.py:102-109`), then appends the tool results as a `user` message (`agent.py:110`). The
next iteration therefore re-reasons over the observations the previous one produced — that is
the re-planning mechanism, and it is implicit in the message list rather than a separate
planner component.

**Stop conditions — exactly two, both explicit:**
1. The model stops requesting tools → `agent.py:96-98` returns the assembled text.
2. Iteration budget exhausted → `agent.py:112-113` returns a truthful failure string:
   `"(I hit my iteration limit before finishing — try breaking the request into smaller steps.)"`.

**Budgets (quantified):**

| Budget | Value | Source |
|---|---|---|
| `max_iterations` | 10 | `waku/loop/agent.py:47`, env `WAKU_MAX_ITERATIONS` at `waku/config.py:36` |
| `max_tokens` per call | 8192 | `waku/config.py:42` (raised from 2048 because reasoning models were hitting `stop_reason=max_tokens` mid-thought — comment at `config.py:38-42`) |
| history window | 12 turns = 24 messages | `waku/config.py:48`, applied at `waku/app.py:66-67` (`window = history_turns * 2`) |
| LLM call timeout | 120 s | `waku/loop/models.py:141` |

**How it handles a FAILED tool call mid-loop — the critical difference.**
`waku/tools/registry.py:47-58`:

```python
def execute(self, name, args, notify=None) -> str:
    tool = self._tools.get(name)
    if tool is None:
        return f"Error: unknown tool '{name}'"
    try:
        ...
    except Exception as exc:      # surface, don't crash — the model can retry
        return f"Error running {name}: {exc}"
```

The exception is converted into the tool's *observation string*. It flows into
`tool_results` (`agent.py:107-109`) and back into the model's context on the next
iteration. The model sees "Error running create_event: …" and can retry with different
arguments, call a different tool, or explain the failure to the user. An unknown tool name
is likewise an observation, not a crash (`registry.py:52`). A streaming transport failure
is also absorbed: `agent.py:77-78` sets `response = None` on any streaming exception and
falls through to a single non-streaming call at `agent.py:79-86`.

**AegisOps has no equivalent.** The graph is a fixed single-pass DAG:
`backend/app/agents/graph.py:96-108` wires 12 nodes with conditional branching only at
`router` (`graph.py:97`), the three plan nodes (`graph.py:99-100`), and `approval`
(`graph.py:103`). There is no edge that returns to a planning node, so no node can observe
a tool result and re-decide. A grep for iteration control across `backend/app` returns hits
in exactly one file — `exec_loop.py` — and nothing else.

The nearest analogue is the **Governed Executive Loop**, `backend/app/agents/exec_loop.py`,
and it is deliberately not an agentic loop:

- `exec_loop.py:36-37` — `MAX_STEPS = 5`, `MAX_REPLANS_PER_STEP = 1`.
- `exec_loop.py:46` — `_replan_step: Callable = lambda step, observation: None`. The default
  replanner **always returns None**, so no replan ever occurs; a failed step halts.
- `exec_loop.py:284-298` — the `while True` around a step is a retry-after-replan construct,
  and because `_replan_step` returns None it executes each step exactly once.
- A failed step becomes an honest observation (`exec_loop.py:258-264`, status `"failed"`), and
  `_partial_outcome` (`exec_loop.py:334-354`) reports e.g. *"steps 1, 2 applied; step 3 failed:
  … 1 later step(s) were not attempted."* Later steps are never attempted blind.
- Any revision to an approved step is a **deviation** → a fresh approval interrupt
  (`exec_loop.py:303-311`).
- It is **off by default**: `backend/app/settings.py:47` — `aegisops_exec_loop: Literal["on","off"] = "off"`,
  gated at `backend/app/agents/cloudops.py:618`. In the default posture, the create-first DAG
  is only *described* as text (`cloudops.py:623-637`).

So AegisOps' multi-step path is a **deterministic pipeline over a pre-computed DAG**, not a
loop. That is a defensible governance choice for mutation. What it means, though, is that
AegisOps has *no* iterative reasoning capability anywhere — including on read-only paths
where waku's loop would be safe and useful (§3).

### 1.2 Native tool-calling and a model-facing tool registry

waku's tools are model-facing by construction: `waku/tools/registry.py:15-34` defines
`Tool(name, description, input_schema, fn, wants_notify)` and `to_api()` emits exactly the
shape the Messages API `tools=` parameter wants. `ToolRegistry.schemas()` (`registry.py:44-45`)
feeds every LLM call at `agent.py:72` and `agent.py:84`. `build_registry`
(`waku/tools/__init__.py:14-74`) assembles 5 flagship tools + 3 memory-self-management tools
+ optional Apple/MCP/experimental tools.

AegisOps' `backend/app/tools/*.py` are **typed Python clients, not tools in the LLM sense** —
`app/tools/aws.py:1` says so outright ("read-only discovery / availability / verification
(boto3). Never provisions."). They are called from agent code by name, never selected by a
model. The Gemini client does accept a `tools=` argument (`app/integrations/gemini.py:103`)
but nothing in the agent path populates it: a grep for `tool_call|function_call|FunctionDeclaration`
across `backend/app` returns that single line. **AegisOps performs zero native tool-calling.**

Its read-only investigation registry (`app/agents/investigation.py`) is the closest thing to
a tool surface, and its director is deterministic by design: `Investigator.run(plan)`
(`investigation.py:120-123`) executes a *caller-supplied* ordered list of
`{"tool": …, "args": …}`. The module comment (`investigation.py:14-16`) is explicit that an
LLM director "would plug in here; today's director is deterministic." In practice the whole
registry is exercised by exactly one hardcoded call — `sre.py:85-86`,
`inv.call("list_deployments", namespace="default")` — out of the 5 registered tools
(`investigation.py:148-155`).

### 1.3 Memory gate (retrieval gating)

`waku/memory/retrieval_gate.py` — one cheap-model call decides **whether to retrieve at all**
before any store is touched.

- `retrieval_gate.py:22-33` — a fixed prompt returning `{"retrieve": bool, "query": str, "reason": str}`.
- `retrieval_gate.py:46` — `max_tokens=600` (raised from 100 because reasoning models spend a
  thinking block before the JSON — comment at lines 44-45).
- **Fails open**: a non-JSON reply returns `(True, message, "gate returned no JSON — failing open")`
  (`retrieval_gate.py:50-51`); any exception returns `(True, message, "gate failed open (…)")`
  (`retrieval_gate.py:54-55`). Stale memory beats lost memory.
- Wired at `waku/memory/__init__.py:58-68`: on `retrieve`, `facts.search(query, top_k=4)` plus
  `episodes.search(query, top_k=3)`; on `skip`, returns `""` and no store is queried.
- The decision is a first-class observable event — `notify("gate", {...})`
  (`waku/memory/__init__.py:62-63`) — surfaced in the CLI (`waku/gateway/cli.py:50`), persisted
  per-turn (`waku/app.py:54-56, 86`), and counted on the dashboard
  (`waku/ops/dashboard.py:315-316`: `gate_skips`, `gate_retrieves`).

**AegisOps retrieves unconditionally.** `backend/app/agents/memory.py:283-289` — whenever
`current_message` is present, `build_context` runs positional-recall detection *and* a
top-k semantic/keyword `retrieve()` every single time. There is no skip decision, no
skip/retrieve counter, and no observable gate event.

### 1.4 Consolidation pass (chat → durable facts)

`waku/memory/consolidation.py:37-75` — a batched distillation pass with a hard due-check.

- Due when unconsolidated `chat_log` rows ≥ `consolidate_every * 2` (`consolidation.py:49`;
  each exchange is 2 rows). Default `consolidate_every = 6` (`waku/config.py:52`) → fires every
  6 exchanges.
- One small-model call, `max_tokens=600` (`consolidation.py:56`), returning
  `{"facts": [{"subject","content"}], "episode": "<one sentence>"}` (`consolidation.py:30-31`).
- Writes N facts with `source="consolidation"` (`consolidation.py:64-66`) and one dated episode
  (`consolidation.py:67-68`), then marks exactly the rows it read as consolidated
  (`consolidation.py:70-73`).
- **Never loses data on failure**: a parse/API error returns `0` and leaves the log
  unconsolidated for the next attempt (`consolidation.py:61-62`).
- Invoked after every turn (`waku/app.py:98-100`) and emits `notify("consolidation", {...})`
  (`waku/memory/__init__.py:168-169`).
- A human-readable mirror is regenerated each turn: `MEMORY.md` with fact and episode counts
  (`waku/memory/__init__.py:133-157`).

**AegisOps has no consolidation, no distillation, and no episodic tier.** Its memory is
raw-transcript-plus-retrieval only. Schema comparison:

| Tier | waku | AegisOps |
|---|---|---|
| Raw log | `chat_log` — 7 cols (`id, role, content, consolidated, session_id, source, meta`), `waku/db.py:68-75` + `db.py:82-95` migrations | `messages` — 14 cols incl. `embedding Vector`, `confidentiality_level/score`, `trace_id`, `context_id`, `run_id`, `analysis JSONB` (`backend/app/db/models.py:86-107`) |
| Semantic facts | `facts` — 5 cols + `facts_fts` FTS5 with 3 sync triggers (`waku/db.py:26-45`) | **none** |
| Episodic | `episodes` — 4 cols + `episodes_fts` (`waku/db.py:48-62`) | **none** |
| Procedural | `SKILL.md` files, mtime-hot-reloaded (`waku/memory/procedural/loader.py:47-91`) | **none** |
| Standing user facts | `SOUL.md`, append-only via tool (`waku/tools/memory_admin.py:77-107`, `SOUL_MAX = 8000`) | `user_memory` — 7 cols, org-wide when `user_id IS NULL` (`db/models.py:265-277`); block capped at 600 chars (`app/agents/user_memory.py:22`) |

AegisOps' `messages` row is richer per-row (embeddings, confidentiality, correlation ids —
things waku has no need for), but it has **no tier above the transcript**. Nothing ever
promotes "the user's usual region is ap-south-1, inferred from six prior runs" into durable
knowledge; `user_memory` is only ever written by explicit human action.

### 1.5 Self-managing memory tools

`waku/tools/memory_admin.py` gives the *agent* three tools over its own memory:
`manage_memory` (search/update/delete facts and episodes, `memory_admin.py:25-74`),
`update_soul` (append one behaviour rule; append-only so the agent cannot delete its own
honesty rules, `memory_admin.py:77-107`), and `create_skill` (author a validated `SKILL.md`,
refusing to overwrite an existing one, `memory_admin.py:110-144`). AegisOps has no agent-facing
memory-write surface at all — `user_memory.set_memory` (`app/agents/user_memory.py:32-47`) is
reachable only from human-driven API paths.

### 1.6 LLM-as-judge evals + a score-gated release gate

waku has a two-axis eval system and a gate that can actually block a release.

**Axis 1 — deterministic (0/1, no judge).** 35 files, 208 test functions in
`evals/deterministic/`. The scorer is a single shared pure function,
`waku/ops/scoring.py:32-48` (`check_case`), asserting: the expected tool fired, expected
substrings appear in the recorded args, and a minimum tool-call count was reached. Cases come
from `evals/dataset.jsonl` — **11 cases**, including 4 multi-tool cases with
`expect_min_tool_calls` of 2 or 3 and 2 negative cases with `expect_tool: null`
("What is the capital of France?", "I might grab coffee with Alex sometime"). Keeping the
scorer in one module is deliberate so the CLI table and the on-screen scoreboard cannot drift
(`scoring.py:5-11`).

**Axis 2 — LLM-as-judge (scored %, thresholded).** `evals/judge/test_response_quality.py`
defines 2 DeepEval `GEval` metrics — Helpfulness (`threshold=0.6`, line 37) and MemoryUse
(`threshold=0.6`, line 50) — judged through `evals/judge/anthropic_judge.py`, which reuses
waku's own provider client so no separate judge key is needed.

**The gate.** `waku/ops/release_gate.py:62-88`:
- deterministic suite must pass; a non-zero exit writes `report("fail","not run")` and
  `sys.exit(1)` with `"GATE CLOSED — deterministic evals failed."` (lines 65-69);
- the judge suite runs only when the active provider's key exists (lines 77-78); failure →
  `"GATE CLOSED — judge scores below threshold."` and `sys.exit(1)` (lines 79-82);
- no key → honest `report("pass","skipped")` (line 85);
- every verdict is persisted twice — latest to `eval_report.json`, appended to
  `eval_runs.jsonl` (`release_gate.py:57-59`) — so the dashboard can show gate history
  (`waku/ops/dashboard.py:266-274`, last 20 runs).

**A second judge, for model comparison.** `waku/ops/judge.py` grades a reply 0-10 against a
rubric (`judge.py:37-57`) with two production-grade details worth naming: a concurrency
semaphore (default 2, `judge.py:35`) so a burst of judge calls isn't 429'd, and 4 attempts
with growing backoff of 1.2 s / 2.4 s / 3.6 s that retries **only** the API call, never a
response that arrived but won't parse (`judge.py:82-101`). The rubric is fed the list of tools
that *actually fired* as ground truth (`judge.py:71-73`) so a truthful "I saved that" isn't
scored as a hallucination — an explicit anti-false-positive measure (`judge.py:49-54`).

**AegisOps has no judge, no eval dataset, and no score-gated release.** A grep for
`judge|rubric|GEval|deepeval` across `backend/app` and `backend/tests` returns one irrelevant
comment (`backend/tests/test_modseed_ms7_aws_rds.py:181`). Its 596 tests are all binary
correctness/invariant assertions — excellent ones (`test_safety_invariants.py`,
`test_tenancy.py` at 587 LOC), but they cannot answer *"did answer quality regress when we
changed the router prompt?"* CI (`.github/workflows/ci.yml`) runs ruff, pytest, frontend
lint/typecheck/vitest/build, `docker compose config`, and a Terraform security scan
(checkov 3.3.8 + tfsec 1.28.14) — solid engineering gates, zero behavioural-quality gates.

### 1.7 Per-iteration context assembly with an explicit recipe

waku builds the prompt from a short, readable recipe every turn
(`waku/runtime/session.py:63-88`):

1. `SOUL.md` — the editable persona, created on first run (`session.py:44-50`);
2. local wall-clock time with timezone (`session.py:68-70`) so relative dates resolve without
   asking;
3. the agent's own model/provider identity (`session.py:72-75`);
4. gated memory, only if the gate said retrieve (`session.py:81-83`);
5. matching skill bodies, only if a skill matched (`session.py:84-86`).

Then, in `waku/app.py:66-67`, the *message list* is windowed to the last
`history_turns * 2 = 24` messages. Cost and latency stay flat no matter how long a
conversation runs; older turns are recoverable through consolidation + the gate. Tool
activity is folded into each history entry as a compact `[tools used: …]` line
(`waku/runtime/session.py:99-102`) — added specifically to stop the model re-running a tool it
already ran (the "triple-booked meeting" bug named in the docstring at `session.py:96-98`).

**AegisOps' context assembly is in some ways more sophisticated**, and in one way weaker.
`backend/app/agents/memory.py:261-298` (`build_context`) composes: standing user memory →
exact positional recall → top-k semantic hits → transcript. Its budgets are per-purpose
(`memory.py:257-258`), in characters (`chars ≈ 4 · tokens`):

| purpose | budget (chars) |
|---|---|
| `router` | 1,600 |
| `cloudops` / `devops` / `sre` | 3,000 |
| `knowledge` | 4,000 |
| `loop` | 4,000 |
| `general` | 8,000 |

Its long-thread strategy is genuinely better than waku's flat window: `build_transcript`
(`memory.py:196-245`) spends ~70 % of budget on the newest turns verbatim and the remainder on
a digest of **every** older user turn, so early facts stay recallable at turn 50 —
waku simply drops turn 13. It also has exact positional recall via regex
(`memory.py:35-45`, `detect_recall` at `memory.py:91-106`) and pgvector-cosine retrieval with
a `pg_trgm` fallback (`memory.py:112-146`).

The weakness is *frequency*: this assembly runs **once per graph node**, against a fixed
transcript. There is no per-iteration re-assembly because there are no iterations. Nothing
re-packs the window after a tool observation lands.

### 1.8 Live flow dashboard (architecture that lights up)

`waku/ops/dashboard.py` is 893 lines on the Python stdlib — `ThreadingHTTPServer` bound to
`127.0.0.1:7777` (`dashboard.py:866`), walking past a busy port rather than crashing
(`dashboard.py:864-868`), serving plain static files with no build step
(`dashboard.py:774-783`).

What AegisOps has no analogue for:

- **A live architecture SVG that animates as a turn flows through it.**
  `waku/ops/static/js/diagram.js:11-97` renders the harness diagram; `diagram.js:105-112`
  maps trace event types to node/edge ids (`turn_start → [gateway, wm]`, `gate → [gate]`,
  `llm → [llm]`, `tool → [tools]`, `turn_end → [reply, trace]`,
  `consolidation → [consolidation, semantic]`); `animateStage` (`diagram.js:121-131`) lights
  them for 1,000 ms with a 620 ms stagger (`diagram.js:137`). A `retrieve` gate decision
  additionally lights all three memory pillars (`diagram.js:127-130`).
- **Gateway-agnostic liveness.** The animation is driven by polling `/api/events` with a
  line-count cursor over today's trace file (`dashboard.py:710-732`, `diagram.js:139-148`), so a
  message arriving via CLI, voice, Telegram, or Discord lights up the browser diagram.
  `cursor=None` deliberately returns only the current tail so a fresh page doesn't replay
  history (`dashboard.py:724-725`).
- **Hang forensics.** A turn with `turn_start` and no `turn_end` is rendered as
  `"TURN NEVER FINISHED — check for a hang after this point"` with `unfinished: true`
  (`dashboard.py:224-227`).
- **A read-only SQL console over live state** — opens the DB `mode=ro`, single statement,
  `SELECT`/`WITH` only, 200-row cap (`dashboard.py:494-521`).
- **Schema introspection as a first-class view** — `PRAGMA table_info`, row counts, and 200
  sample rows per table, plus the FTS table list (`dashboard.py:282-300`).
- **A permanent token ledger separate from resettable traces.** `usage.jsonl` is appended per
  LLM call and never wiped; dollars are derived from tokens because prices change
  (`waku/ops/tracing.py:103-114`). Sub-agent spend lands in the *same* ledger with
  `kind="subagent"` (`waku/tools/experimental.py:85-97`) — added because delegated runs were
  burning tokens the scoreboard never counted (`experimental.py:19-22`).
- **A model arena.** `waku/ops/arena.py:35-179` races N models through the *real* harness, each
  in its own throwaway home dir (`arena.py:73`) so races never touch real data,
  `ThreadPoolExecutor(max_workers=min(len(specs), 6))` (`arena.py:150`), then grades in one
  gentle pass at `max_workers=2` *after* the race so the referee isn't stampeded
  (`arena.py:153-172`).

AegisOps' UI is a polished 8-tab artifact panel — `timeline`, `terraform`, `reasoning`,
`logs`, `metrics`, `traces`, `references`, `approvals`
(`frontend/components/ArtifactPanel.tsx:108-115`) — backed by real per-step timings persisted
to `run_steps` (`backend/app/agents/timing.py:57-113`). It is a better *record* of a run. It
is not a live *picture of the system*: there is no architecture diagram, no node-level
animation, no cross-channel liveness, and no equivalent of the "turn never finished" tell.

### 1.9 Multi-channel gateway

waku ships five gateways behind one `respond()` (`waku/__main__.py:17-50`): CLI
(`waku/gateway/cli.py`), voice (`waku/gateway/voice.py`, 360 LOC — local faster-whisper ears,
macOS `say`/Kokoro mouth, and a wake-word loop at `voice.py:228`), Telegram
(`waku/gateway/telegram.py`), Discord (`waku/gateway/discord.py`), and the dashboard. A gateway
only moves text; the harness is identical for all of them (`waku/gateway/cli.py:3-6`). The
dashboard can host Telegram and Discord pollers on daemon threads so one command runs
everything (`waku/ops/dashboard.py:870-885`), and a gateway failure is caught and printed
rather than taking the dashboard down (`dashboard.py:877-885`).

Two security details are worth naming because they are *harder* than a default:
`telegram.py:32-39` prints the reachability posture at every startup, in words, when no
allowlist is set ("reachable by: ANYONE who finds this bot — it will answer from your personal
memory"); and `discord.py:80-103` (`should_answer`) is a pure, unit-testable predicate whose
docstring calls out the load-bearing line — an *unset* channel allowlist must not mean "all
channels" (`discord.py:90-95`).

AegisOps has one channel in (the web UI over `POST /chat` SSE) and one channel out (in-app
notification always, SMTP email when configured — `backend/app/agents/notify.py:38-60`,
recipients = initiator + approver, `notify.py:22-35`). There is no Slack/Teams/chat-ops surface
and no voice.

### 1.10 Other things found in waku's code with no AegisOps counterpart

- **Provider portability.** 11 provider entries across 2 wire formats
  (`waku/loop/models.py:54-109`); the entire Anthropic↔OpenAI bridge is ~110 lines
  (`models.py:153-267`), including a `max_tokens`/`max_completion_tokens` fallback that only
  retries when the error is *about that parameter* (`models.py:211-226`) and preservation of
  Gemini's `thought_signature` across turns, without which follow-up calls 400
  (`models.py:245-249`). Key hygiene is checked at startup: keys are `.strip()`ed and
  latin-1-validated with a plain-English error about smart quotes (`models.py:122-134`).
- **Procedural memory with progressive disclosure.** Frontmatter of every skill is always
  scanned; a body loads only on a keyword-overlap match of ≥2 tokens, max 2 skills
  (`waku/memory/procedural/loader.py:77-91`), with mtime-signature hot reload so a skill created
  mid-session is live next turn (`loader.py:58-64, 81`).
- **Observer/tracer as one interface.** `Observer = Callable[[str, LoopEvent], None]`
  (`waku/loop/agent.py:31`); `Tracer.event` *is* an observer (`waku/ops/tracing.py:57-59`) and
  `compose()` fans one event to many sinks (`tracing.py:161-167`). One seam serves live UI,
  JSONL trace, and OTel spans.
- **Sub-agent delegation with live relay and honest cost.**
  `waku/tools/experimental.py:184-314` delegates coding work to `pi`, probing once for
  `--mode json` support (`experimental.py:53-62`), relaying its event stream through the loop's
  observer as `kind="subagent"` (`experimental.py:142-167`), and enforcing the deadline with a
  reader thread + `queue.get(timeout=…)` because a blocking `readline` can't be interrupted
  (`experimental.py:106-133`).
- **Session rotation on idle.** `waku/ops/browser_agent.py:100-118` — come back after
  `WAKU_SESSION_IDLE_MINUTES` (default 60) and you get a fresh thread; the docstring records
  the live bug that motivated it (a tester's message landing in a week-old 32-message thread).
- **Encoding-safety over data.** A legacy non-UTF-8 trace file raises `TraceEncodingError`
  with recovery instructions and the file is **not modified** (`waku/ops/tracing.py:35-45, 90-101`).

---

## 2. Which harness is better — pillar by pillar

### Pillar 1: The loop — **waku wins, decisively**

| | waku | AegisOps |
|---|---|---|
| Real iteration | yes, `agent.py:63`, budget 10 | no; fixed DAG, `graph.py:96-108` |
| Re-plan after observation | yes, implicit via message list `agent.py:91,110` | no; `_replan_step` returns `None` by default (`exec_loop.py:46`) |
| Failed tool mid-run | becomes an observation, model retries (`registry.py:57-58`) | honest halt + partial report (`exec_loop.py:296-298, 334-354`) |
| Bounds | `max_iterations=10`, `max_tokens=8192` | `MAX_STEPS=5`, `MAX_REPLANS_PER_STEP=1` (`exec_loop.py:36-37`), off by default (`settings.py:47`) |

waku's loop can recover from a failure inside a single turn; AegisOps cannot recover at all —
a failed step ends the run with a report. For *mutation*, AegisOps' choice is correct and I
would not change it. For *investigation and read paths*, it is a straight capability gap:
AegisOps cannot chase a symptom across three tools and revise its hypothesis, because nothing
in the codebase can loop.

### Pillar 2: Memory — **split; waku wins on architecture, AegisOps on per-row rigor**

waku has 4 tiers (procedural / semantic / episodic / raw log — `waku/db.py:12-76`,
`waku/memory/__init__.py:1-10`) plus two managing agents (gate, consolidation) and three
agent-facing write tools. AegisOps has 2 tiers (raw `messages`, human-set `user_memory`) with
no gate, no distillation, no episodic layer, and no agent write path.

AegisOps wins on what a single row carries and on long-thread recall: `messages` has 14
columns including a pgvector `embedding`, confidentiality level/score, and full correlation ids
(`db/models.py:86-107`); `build_transcript` (`memory.py:196-245`) keeps a digest of every older
user turn instead of dropping it; positional recall is exact and deterministic
(`memory.py:77-106`), which waku has no equivalent for. Multi-tenancy is enforced at the row
level (`user_memory.py:38-47` upserts under `org_id`).

Verdict: waku's *shape* is right (gate + tiers + consolidation), AegisOps' *substrate* is
right (Postgres, org-scoped, embedded, correlated). The gap is the two missing agents — a
gate and a consolidator — not the storage.

### Pillar 3: Tools — **AegisOps wins on safety, waku on capability**

AegisOps' tool surface is safe by construction. `investigation.py:49-56` (`assert_read_only`)
rejects any tool whose name contains one of 20 mutation markers **at registration time**;
`ToolRegistry.freeze()` (`investigation.py:72-74`) makes a running investigation unable to grow
its surface; `spawn()` shares the parent's `_calls_used` list (`investigation.py:102, 125-128`)
so a sub-agent can go no wider and no deeper. Budget: `MAX_CALLS = 8` (`investigation.py:28`).
That is a genuinely better *safety* design than waku's registry, which has no read-only
assertion at all (`waku/tools/registry.py:41-42` registers anything).

But AegisOps' registry is barely used — one hardcoded call at `sre.py:85-86` out of 5
registered tools, with a deterministic director. waku's registry is the live spine of every
turn, with 8+ tools, MCP-server bridging (`waku/tools/__init__.py:61-72`), and a
`wants_notify` streaming seam for long-running tools (`registry.py:26`, `registry.py:54-55`).

Verdict: AegisOps has the better *boundary*, waku has the better *engine*. These compose —
which is the whole thesis of §3.

### Pillar 4: Evals — **waku wins, decisively**

| | waku | AegisOps |
|---|---|---|
| Deterministic outcome tests | 208 fns / 35 files, shared scorer (`scoring.py:32-48`) | 596 fns / 93 files |
| Behavioural dataset | 11 cases, `evals/dataset.jsonl` | none |
| LLM-as-judge | 2 GEval metrics @ `threshold=0.6` (`test_response_quality.py:37,50`) + 0-10 rubric judge (`judge.py:37-57`) | none |
| Release gate | `release_gate.py:62-88`, exit 1 on either axis | CI: lint/test/typecheck/build/compose/tfsec — no quality axis |
| Verdict history | `eval_report.json` + `eval_runs.jsonl` (`release_gate.py:57-59`) | none |

AegisOps has 2.9× the test functions and they cover things waku cannot dream of (tenancy
isolation, safety invariants, plan-guard). But it has **zero** regression protection on answer
quality. Change the router prompt in `router.py:26-58` and nothing in CI notices a
classification regression until a human sees it in production. waku, at a fraction of the
size, cannot ship a prompt change without clearing a threshold.

### Pillar 5: Tracing / ops — **AegisOps wins on depth, waku on immediacy**

AegisOps' tracing is production-grade in ways waku's is not:
- **The trace id *is* the run id**, so an approval resume re-attaches to the same trace
  (`backend/app/integrations/langfuse_client.py:105-118`, `runner.py:44-56`).
- **Deterministic span ids** `<run_id>:<name>` (`langfuse_client.py:75-76`) let a span opened
  before a human interrupt be closed by a *different process* after the decision, recording the
  true wall-clock wait (`langfuse_client.py:153-173`, `timing.py:57-79`).
- **Redaction on every payload leaving the process** (`langfuse_client.py:62-72`), plus a
  persistence-path backstop on `messages.content` and `runs.outcome`
  (`api/chat.py:160-170`).
- **Misconfiguration is caught loudly**: `assert_project` (`langfuse_client.py:286-321`) detects
  keys that belong to a *different* Langfuse project — the exact cause of a historical
  "0 traces" regression — and returns a testable status string.
- **Real Prometheus series**: `AGENT_STEP_DURATION` observed per step (`timing.py:103-107`),
  `APPROVAL_WAIT` labelled by domain+decision (`api/chat.py:336-345`), `STRANDED_RUNS`,
  `DRIFT_FINDINGS`, `RECONCILER_SWEEP_FAILURES` (`reconciler.py:220-232`).

waku's tracing is a JSONL file plus optional OTel (`waku/ops/tracing.py:1-18`) with
per-turn flush so a killed process keeps its trace (`tracing.py:156-158`). Shallower — but it
is *always on with zero dependencies*, readable with `cat`, and it drives a live diagram. And
it has one thing AegisOps lacks entirely: a **permanent token/cost ledger** distinct from
resettable traces (`tracing.py:103-114`).

Verdict: AegisOps wins the pillar. waku wins the two sub-points of always-on-zero-dep
visibility and cost ledger.

### Pillar 6: Context assembly — **AegisOps wins on technique, waku on cadence**

AegisOps' assembly is objectively more advanced: per-purpose budgets across 7 purposes
(`memory.py:257-258`), a 70/30 recent-verbatim + older-digest split that keeps early facts
alive (`memory.py:216-245`), exact positional recall (`memory.py:77-106`), semantic retrieval
with a keyword fallback (`memory.py:112-146`), and **context offloading** — a large plan is
referenced by a one-line summary, never inlined, and fetched on demand
(`memory.py:152-169`, `plan_ref_line`/`fetch_plan`).

waku's is simpler — SOUL + time + identity + gated memory + skills, then a flat 24-message
window (`session.py:63-88`, `app.py:66-67`). But it is re-assembled *per turn* and its
`[tools used: …]` fold-in (`session.py:99-102`) solves a real duplicate-action bug that
AegisOps has no analogue for.

Verdict: AegisOps wins. The gap it should close is not technique but *when* assembly happens —
per-node today, never per-iteration.

### 2.7 What AegisOps has that waku lacks (honest accounting)

These are requirements differences, not waku defects. waku is a single-user local assistant;
none of this is in its job description. Grepping waku for `rbac|tenant|org_id|approval|audit_log|oidc|keycloak`
across `waku/**.py` returns **zero hits**; so does `terraform|boto3|kubernetes|azure`.

| Capability | AegisOps evidence | waku |
|---|---|---|
| **Multi-tenancy** | `security/tenancy.py:46-100` — strict resolution, refuses a principal with no org (line 79); `settings.py:32` default `strict`; every query org-scoped; `tests/test_tenancy.py` = 587 LOC | none |
| **RBAC** | 8 roles, 3 capability tiers (`security/rbac.py:17-48`); enforced at endpoints (`api/chat.py:212, 350`) and re-checked per tool | none |
| **Human-approval interrupt** | `agents/approval.py:35-92` — LangGraph `interrupt` (line 58) over a durable checkpoint; immutable `Approval` row (73-81); resumed by `POST /approvals/{run_id}` (`api/chat.py:348`) | none |
| **Four-eyes on Production** | `api/chat.py:362-367` — the initiator of a Production change cannot approve it | none |
| **Terraform-only mutation** | `tools/terraform.py` (492 LOC) does every mutation; cloud SDKs are read-only by module contract (`tools/aws.py:1`) | none |
| **Action/plan hard guard** | `agents/plan_guard.py:36-77` — pure function; create may not delete/replace, modify may not replace, destroy may not create, read may not plan; re-asserted at the approval choke-point (`approval.py:44-51`) | none |
| **Durable checkpoints + crash recovery** | Postgres checkpointer (`graph.py:110`); heartbeat TTL 45 s / refresh 15 s (`supervisor.py:24-25`); reconciler sweeps every 60 s and resumes from checkpoint or fails honestly (`reconciler.py:34, 66-73`); graceful drain (`supervisor.py:127-145`) | none |
| **Idempotency** | claim/wait-or-abort with a 20 s deadline (`security/idempotency.py:51-68`); applied per run (`cloudops.py:1430-1444`) and per DAG step (`exec_loop.py:209-215`) — an already-applied step returns its stored result, never re-applies | none |
| **Cooperative cancel, never mid-apply** | `supervisor.py:39-46`, honored at step boundaries (`exec_loop.py:279-282`), terminal `cancelled` (`api/chat.py:126-138`) | none |
| **Drift / world model** | `agents/drift.py:193-273` — drift + `deleted_outside` + orphan sweep, curated fields per type (`drift.py:42-48`), 24 h Redis dedupe (`drift.py:147-168`); `graph_db/world_model.py:157-175` `impact_of` answers "what depends on this?" before a destroy, from *pure lookups* over real inputs (`world_model.py:27-37`) — never LLM-inferred | none |
| **Confidentiality classification** | `security/confidentiality.py:14-46` — 10 weighted signal patterns → Low/Medium/High per message, persisted per row | none |
| **Secret redaction** | `security/redaction.py`, applied on trace/graph/persistence paths (`langfuse_client.py:62-72`, `api/chat.py:162-170`) | none |
| **Approved-module catalog** | 21 `WorkflowTemplate`s (`agents/templates.py:455-478`), each with a schema + policy function; `exec_loop.py:93-97` refuses any step not in the catalog ("never generated infrastructure code") | none |
| **Horizontal scale** | Redis-Streams event bus so any worker can serve any run's stream (`agents/events.py:68-155`), with a cursor fix so an approval continuation tails from *now* (`events.py:90-100`) | single-process |
| **Concurrency limits** | `max_active_runs_per_org=5`, `per_user=2` (`settings.py:193-194`), derived from heartbeat liveness rather than a drift-prone counter (`api/chat.py:79-106`) | none |
| **Provider-failure triage** | `agents/provider_errors.py:29-123` — 7 classified failure kinds → title/cause/next-step, deterministic from error text; plus one-click retry-with-fix (`provider_errors.py:136-156`) | none |
| **ITSM integration** | real ServiceNow SR/CR/Incident per actionable intent (`agents/router.py:191-210`) | none |

On any pillar that matters for shipping infrastructure change to a paying customer, AegisOps
is not merely ahead — waku has no entry.

### 2.8 BOTH-WEAK — gaps neither system solves

1. **No LLM director over the safe tool surface.** waku has a loop but no read-only boundary
   (`waku/tools/registry.py:41-42` registers anything). AegisOps has the boundary
   (`investigation.py:49-74`) but no director — `Investigator.run(plan)` executes a
   caller-supplied list (`investigation.py:120-123`) and the module comment admits the director
   "is deterministic" (`investigation.py:14-16`). Neither ships a *bounded, read-only,
   model-directed* loop. This is the single most valuable thing that doesn't exist in either
   repo.
2. **No cost budget that can stop a run.** Both *measure* tokens — waku permanently
   (`tracing.py:103-114`), AegisOps per-generation with derived USD
   (`langfuse_client.py:198-214`). Neither enforces a ceiling. No `max_cost_per_run` exists in
   either codebase; waku bounds iterations, AegisOps bounds steps, and both let a single
   expensive turn run to completion.
3. **No prompt/config versioning tied to eval results.** waku's gate records a verdict
   (`release_gate.py:51-59`) but not *which prompt* produced it. AegisOps has
   `workflow_version` on runs (`agents/state.py:31`) but no prompt version anywhere. Neither
   can answer "which prompt revision caused this regression?"
4. **Sub-agent budget isolation is unproven.** waku's `delegate_task` spawns `pi` with a wall
   clock only (`experimental.py:214`), no token ceiling. AegisOps' `spawn()` shares a call
   budget (`investigation.py:125-128`) but is never actually called anywhere in the codebase.
5. **Memory conflict resolution is absent.** waku's consolidation appends facts
   (`consolidation.py:64-66`) with no contradiction check — "Alex prefers mornings" and "Alex
   prefers evenings" coexist and both retrieve. AegisOps' `user_memory` upserts by key
   (`user_memory.py:40-46`), which silently overwrites rather than reconciling. Neither
   detects or surfaces a contradiction.
6. **Retrieval quality is unmeasured.** waku's gate decides retrieve/skip but nothing scores
   whether retrieved facts *helped*; its one MemoryUse metric
   (`test_response_quality.py:38-51`) is a 2-case smoke test. AegisOps retrieves `k=3`
   (`memory.py:286`) with no precision/recall instrumentation at all.
7. **Thin general audit coverage.** AegisOps has an insert-only `audit_log` table
   (`db/models.py:213-225`) and `AuditRepo.log` (`db/repositories.py:60-68`), but it is
   written from only **two** call sites — `admin.py:149` and `api/artifacts.py:311`.
   Approvals are audited in a dedicated immutable `Approval` table instead
   (`approval.py:73-81`), which is stronger where it applies; but chat runs, cancels, and
   memory edits leave no `audit_log` row. waku has no audit concept at all.
8. **No structured self-evaluation inside a run.** Neither system asks "did I actually
   accomplish the goal?" before replying. waku returns whatever the model says; AegisOps
   verifies infrastructure facts via SDK reads (`cloudops.py` verify path) but never evaluates
   its own *answer*.

---

## 3. What to inherit from waku into AegisOps

**Hard constraint honored throughout.** AegisOps' invariants are untouchable: Terraform-only
mutation, the human-approval interrupt, RBAC/tenancy, durable checkpoints, audit. Every
adoption below is either (a) confined to read-only paths over the INV registry, or (b) an
offline/observability capability that touches no mutation path. Nothing below lets a model
select or author a mutation: mutations continue to exit only through the approved catalog
(`agents/templates.py:455-478`) and the approval gate (`agents/approval.py:35-92`).

| # | Pattern (waku source) | Where it lands in AegisOps | What it improves | Effort | Value | Decision |
|---|---|---|---|---|---|---|
| 1 | **Iterative loop with iteration budget + failed-tool-as-observation** (`waku/loop/agent.py:63-113`; `waku/tools/registry.py:47-58`) | NEW `backend/app/agents/inv_loop.py`, driven by `investigation.Investigator` (`investigation.py:104-128`); called from `sre.py:_collect_telemetry` (`sre.py:77-93`) and `cloudops._read_path` (`cloudops.py:925`) | Multi-hop triage: chase a symptom across Prometheus → deployments → pods → impact graph, revising after each observation, instead of one hardcoded `list_deployments` call | M | **H** | **ADAPT** — bounded by the *existing* `MAX_CALLS = 8` (`investigation.py:28`) plus a new `MAX_INV_ITERATIONS`; the loop's tool surface is the frozen read-only registry (`investigation.py:72-74`), so `assert_read_only` (`investigation.py:49-56`) already makes mutation unreachable. Runs only on read/triage paths; never on `cloudops_plan`/`execute`. |
| 2 | **Retrieval gate** (`waku/memory/retrieval_gate.py:36-55`) | `backend/app/agents/memory.py:261-298` — wrap the `retrieve()` call at `memory.py:286` | Kills a Gemini call + a pgvector query on every turn that doesn't need history; removes irrelevant-context bias | S | **H** | **ADAPT** — keep waku's fail-open (`retrieval_gate.py:50-55`); add a deterministic pre-check so a positional-recall hit (`memory.py:281-285`) or a live `params.load_pending` collection (`router.py:75`) **always** retrieves regardless of the gate. Emit the decision as an SSE `step`/Langfuse event, mirroring `notify("gate", …)` (`waku/memory/__init__.py:62-63`). |
| 3 | **LLM-as-judge evals + score-gated release** (`evals/judge/test_response_quality.py:20-52`; `waku/ops/judge.py:37-101`; `waku/ops/release_gate.py:62-88`) | NEW `backend/evals/` (dataset + judge + gate), invoked from `.github/workflows/ci.yml` as a job after `backend` | The only defense against a router/prompt quality regression; today `router.py:26-58` can be edited with zero behavioural coverage | M | **H** | **ADOPT** — with waku's two production details carried over: the judge concurrency semaphore (`judge.py:35`) and retry-only-on-API-error (`judge.py:82-101`). Judge runs against recorded transcripts; it never touches infrastructure. |
| 4 | **Deterministic outcome dataset + one shared scorer** (`evals/dataset.jsonl`; `waku/ops/scoring.py:32-48`) | NEW `backend/evals/dataset.jsonl` + `backend/app/evals/scoring.py` | Locks routing/classification behaviour: assert `domain`, `action`, `target`, and template selection per case — including negative cases (`expect_tool: null`) that catch the read→destroy class of bug `intent_guard` exists to prevent | S | **H** | **ADAPT** — waku scores *tool fired*; AegisOps should score *classification + template + guard outcome*, which is the equivalent observable. Keep the one-scorer rule (`scoring.py:5-11`) so CI and any UI scoreboard cannot drift. |
| 5 | **Consolidation pass** (`waku/memory/consolidation.py:37-75`) | NEW `backend/app/agents/consolidation.py`; scheduled from `agents/reconciler.py:213-234` (the existing sweep loop); writes to `user_memory` (`db/models.py:265-277`) | Standing facts stop being purely manual: "usual region", "always tag cost-center", "prod changes need the DBA" get proposed from real history | M | M | **ADAPT** — **never auto-write.** Distilled facts land as *proposals* (a `Notification` via `repo.NotificationRepo.create`, as `drift.py:164-166` already does) that a human accepts into `user_memory`. Rationale: an auto-written standing fact would silently change future plan inputs (`cloudops.py:178-184` already honors `usual_region` deterministically), which is a governance change no one approved. Org-scope every write (`user_memory.py:38`). |
| 6 | **Live architecture flow diagram** (`waku/ops/static/js/diagram.js:11-97, 105-131`) | NEW `frontend/components/FlowDiagram.tsx`, fed by the existing SSE `step` events (`agents/events.py:198-199`) and `run_steps` (`timing.py:34-39` already defines canonical node order 0-9) | Operators see *where* a run is and where it stalled, at a glance, instead of reading an 8-tab artifact panel | M | M | **ADOPT** — the data already exists; `timing.ORDER` (`timing.py:34-39`) is the node map. Must respect the pixel-exact design mandate: build it inside the existing artifact-panel shell using only `design-reference` tokens. |
| 7 | **"Turn never finished" hang tell** (`waku/ops/dashboard.py:224-227`) | `frontend/components/ArtifactPanel.tsx` Timeline; data from `run_steps` where `started_at IS NOT NULL AND ended_at IS NULL` (`db/models.py:150-166`) | A stalled step is visible immediately rather than inferred from a 60 s-later reconciler action | S | M | **ADOPT** — pure read over data `timing.py` already writes. Complements the reconciler (`reconciler.py:42-80`) rather than duplicating it. |
| 8 | **Permanent token/cost ledger, separate from traces** (`waku/ops/tracing.py:103-114`) | NEW append-only `llm_usage` table + write from `agents/llm.py:81-91` (`_record`, where usage is already in hand) | Per-org/per-run spend survives a Langfuse outage, retention purge, or key rotation; enables chargeback. Today cost exists only inside Langfuse (`langfuse_client.py:198-214`) | S | M | **ADAPT** — Postgres, org-scoped, not a JSONL file. Also record sub-agent/loop spend so an INV loop's cost is counted (waku's lesson at `experimental.py:19-22`: uncounted delegated spend understated every score). |
| 9 | **Cost/token budget that halts** (gap in **both** systems; waku's iteration bound at `agent.py:112` is the closest analogue) | `backend/app/settings.py` + checked in the INV loop (#1) and at `exec_loop.py:279-282` (the existing cancel boundary) | A runaway investigation stops on budget instead of on wall clock | S | M | **ADAPT** — reuse the *existing* step-boundary halt pattern (`exec_loop.py:279-282`, `_partial_outcome` at `334-354`) so a budget breach reports honestly like a cancel. Never interrupt a Terraform apply. |
| 10 | **Skills / procedural memory as files** (`waku/memory/procedural/loader.py:47-91`) | Would be `backend/app/agents/` prompt assembly | Reusable operator playbooks loaded on match | M | L | **REJECT** — **invariant: approved-catalog-only execution.** A file-driven, hot-reloaded instruction body that steers an agent is an ungoverned change to platform behaviour: `loader.py:81` reloads on mtime with no review, no version, no approval. AegisOps' equivalent already exists and is governed — `WorkflowTemplate` + policy function (`templates.py:455-478`) and the module-promotion pipeline (`agents/module_pipeline.py:215`). Runbook *content* belongs in the RAG corpus (`app/rag/`), where it is org-scoped and cited, not in an executable prompt-injection path. |
| 11 | **Agent self-managing memory tools** (`waku/tools/memory_admin.py:25-144`) | Would be a `user_memory` write tool | Agent corrects its own memory | S | L | **REJECT** — **invariant: audit + tenancy.** `update_soul` is append-only precisely so the agent can't delete its own rules (`memory_admin.py:8-10`), but in AegisOps `user_memory` deterministically alters future plan inputs (`cloudops.py:178-184`). An agent-authored standing fact is an unapproved, unaudited change to what infrastructure gets planned. Covered safely by #5 (human-accepted proposals). |
| 12 | **Model arena / A-B racing in-product** (`waku/ops/arena.py:35-179`) | Would be a new ops surface | Compare models on real tasks | L | L | **REJECT for the product; ADAPT offline.** waku's isolation trick is a throwaway home dir (`arena.py:73`) — there is no equivalent for a shared Terraform state, a real ServiceNow ticket, or an org's inventory. Racing N models through `cloudops_plan` would create N tickets and N plan files. Offline, against the #4 dataset with mocked tool layers, the pattern is fine and folds into #3. |
| 13 | **Multi-channel gateway** (`waku/__main__.py:17-50`; `waku/gateway/*.py`) | NEW `backend/app/api/gateways/` behind the existing `require_initiator` (`api/chat.py:212`) | Slack/Teams chat-ops for approvals and read-only queries | L | M | **ADAPT — read-only + approve-link only.** Any channel must resolve a real Keycloak principal through `security/tenancy.py:46-100` before a run starts; an unauthenticated channel identity can never satisfy `require_approver` (`api/chat.py:350`) or four-eyes (`api/chat.py:362-367`). Ship read-only queries and a deep-link to the web approval UI; never in-channel approval. Note waku's own posture lesson (`telegram.py:32-39`) — an unset allowlist must fail loudly, not open. |
| 14 | **Provider portability seam** (`waku/loop/models.py:54-109, 153-267`) | `backend/app/integrations/llm/registry.py:17` (the seam already exists, 1 provider) | Second provider = failover + cost arbitrage | M | M | **ADAPT** — the abstraction is already there (`integrations/llm/base.py:4-7` says so). Worth doing *after* #3/#4 exist, because swapping providers without a behavioural gate is how a silent quality regression ships. |
| 15 | **Observer/compose fan-out as one interface** (`waku/loop/agent.py:31`; `waku/ops/tracing.py:161-167`) | `backend/app/agents/events.py:188-234` (`Emitter`) | Already effectively present | — | — | **REJECT as new work** — AegisOps' `Emitter` + `runtime.emitter_of` + Langfuse/OTel/Prometheus triple-sink is the same pattern, already more capable (typed methods, two channel backends at `events.py:34-155`). No gap. |

---

## 4. Final verdict

**Adopt specific patterns onto the existing core. Do not restructure AegisOps around waku's
architecture.** The evidence is one-sided.

**Why a restructure would be a mistake.**

A restructure means replacing the LangGraph DAG with a while-loop over a tool registry. Every
governance property AegisOps has is *load-bearing on that DAG*, and none of them survive the
swap:

- The approval gate is a `langgraph.types.interrupt` over a durable Postgres checkpoint
  (`approval.py:58`, `graph.py:110`). It works because a graph node can pause and a *different
  process* can resume it — `api/chat.py:410` calls `run_graph(..., resume=…)`, and the
  reconciler can re-drive the same checkpoint on another worker
  (`reconciler.py:146-201`). A while-loop has no checkpoint to interrupt. waku has zero
  checkpoint machinery: grepping `waku/**.py` for `checkpoint|durable` returns only
  consolidation prose and session-resume, nothing structural.
- Crash recovery is checkpoint-shaped. `reconciler._is_resumable` (`reconciler.py:146-153`)
  asks the graph whether `aget_state().next` is non-empty. Delete the graph and there is no
  question to ask — a run interrupted mid-apply becomes unrecoverable.
- The single-decision approval only makes sense over a *pre-computed* plan. `plan_goal_dag`
  (`exec_loop.py:100-175`) exists so a human approves a bounded, enumerated set of steps.
  A loop that re-plans after each observation cannot show an approver what it will do
  next — which is precisely why `exec_loop.py:303-311` treats any revision as a deviation
  requiring *fresh* approval. Waku's loop re-plans freely because nothing it does needs
  approval; the two designs are answering different questions.
- Idempotency is keyed to graph identity — `idempotency.make_key("tf-exec", run_id, mode)`
  (`cloudops.py:1430`) and `("loop-step", run_id, index)` (`exec_loop.py:209`) — and exists
  because LangGraph re-runs a node body on resume (`exec_loop.py:11-13`). Different execution
  model, different (unwritten) correctness argument.
- Volume: the DAG-shaped machinery under test is ~4,100 LOC of agents plus 12,852 LOC of tests
  (596 functions) including `test_safety_invariants.py` (330 LOC), `test_tenancy.py` (587 LOC),
  `test_exec_loop.py` (231 LOC), `test_retry_undo.py`, `test_pr3_cancel.py`. A restructure
  invalidates the arguments those tests encode.

Against that, waku's total core harness is 2,838 LOC with no tenancy, no auth, no approval, no
checkpoints, and no mutation surface (all four greps empty). It is not a more mature version of
the same thing — it is a different, smaller thing that solved a different problem well.

**Why targeted adoption is the right call.**

The four highest-value waku patterns are all **additive and orthogonal** to the invariants:

1. The **iterative loop** lands *inside* the existing read-only boundary. `investigation.py`
   already provides the frozen registry (`:72-74`), the registration-time mutation denylist
   (`:33-35, 49-56`), the shared call budget (`:102, 125-128`), and the evidence trail
   (`:83-90`). What is missing is only the director — and the module comment says so
   (`investigation.py:14-16`). Adding a bounded LLM director to an already-safe surface changes
   no invariant: `assert_read_only` makes mutation *structurally* unreachable from that
   registry, so a misbehaving loop cannot escalate.
2. The **retrieval gate** wraps one call site (`memory.py:286`). Nothing downstream changes.
3. **Judge evals + a release gate** live entirely in CI. They touch no runtime path.
4. The **flow diagram** consumes SSE events and `run_steps` rows that `timing.py:34-39`
   already produces in canonical order.

None of these require touching `graph.py`, `approval.py`, `plan_guard.py`, `idempotency.py`,
`tenancy.py`, or `terraform.py`.

**The honest asymmetry.** waku's advantage is concentrated in exactly two places — the *loop*
and the *eval gate* — and both are real, both are quantified above, and both are things
AegisOps has zero of, not weak versions of. AegisOps' advantage is everything required to
change real infrastructure for a paying tenant without an incident. The correct move is to take
waku's two wins and leave its architecture alone. AegisOps is not behind on architecture; it is
behind on **iteration inside safe boundaries** and **quality regression protection**.

---

## 5. Top 5 actions (highest value-per-effort, in order)

### 1. A bounded, read-only, model-directed investigation loop

Build `backend/app/agents/inv_loop.py`: a `while` loop that, on each pass, asks Gemini which
registered read-only tool to call next given the evidence so far, executes it through
`investigation.Investigator.call` (`investigation.py:104-118`), appends the `Evidence` to the
context, and repeats until the model stops asking, `MAX_CALLS = 8` is hit
(`investigation.py:28`), or a new `MAX_INV_ITERATIONS` (start at 6) is reached. Copy waku's
single most important line of design: a failed tool call becomes an **observation**, not an
exception — `registry.py:57-58` returns `f"Error running {name}: {exc}"` into the model's next
turn, and `Evidence(ok=False, error=…)` (`investigation.py:116-118`) is already exactly that
shape. Wire it into two call sites only: `sre._collect_telemetry` (`sre.py:77-93`, replacing the
single hardcoded `list_deployments` at `sre.py:85-86`) and `cloudops._read_path`
(`cloudops.py:925`). **What it improves:** AegisOps gains multi-hop reasoning — "error rate is
up" → check deployments → check pod restarts → check what depends on the failing service via
`world_model.impact_of` (`world_model.py:157-175`) → conclude — where today it collects a fixed
signal set and stops. Safety is unchanged and provable: the loop's only tool surface is the
frozen registry, `assert_read_only` (`investigation.py:49-56`) rejects mutation-named tools at
registration, and `spawn()` cannot widen the budget (`investigation.py:125-128`). No mutation
path, no `graph.py` change, no invariant touched.

### 2. Gate the retrieval call

Add `backend/app/agents/retrieval_gate.py` mirroring `waku/memory/retrieval_gate.py:36-55`: one
cheap-model call returning `{"retrieve", "query", "reason"}`, **failing open** on any error or
non-JSON reply (`retrieval_gate.py:50-55`) because stale context beats lost context. Wrap the
`retrieve()` call at `memory.py:286` with it, and keep two deterministic overrides that always
retrieve: a positional-recall hit (`memory.py:281-285`, since the user explicitly asked for turn
N) and an in-flight parameter collection (`params.load_pending`, `router.py:75`). Emit the
decision as an SSE `step` and a Langfuse event so it is as observable as waku makes it
(`waku/memory/__init__.py:62-63`, `dashboard.py:315-316`). **What it improves:** every turn today
pays for an embedding call plus a pgvector query it often doesn't need (`memory.py:283-289` runs
unconditionally), and irrelevant retrieved turns bias answers — the exact failure waku's gate
docstring names (`retrieval_gate.py:4-6`). Small, isolated, one call site, immediately
measurable as a retrieve/skip ratio.

### 3. A behavioural eval dataset + LLM-as-judge + a CI release gate

Create `backend/evals/` with three parts. **(a)** `dataset.jsonl` in waku's shape
(`evals/dataset.jsonl`, 11 cases) but scoring AegisOps' observables: expected `domain`,
`action`, `target`, selected `template.key`, and expected guard outcome — critically including
negative cases ("how many S3 buckets are running?" must be `action=read` and must never produce
a plan) that pin the class of bug `intent_guard.guard_classification` (`router.py:151-160`) and
`plan_guard.check_plan_actions` (`plan_guard.py:36-77`) exist to prevent. **(b)** One shared pure
scorer, `backend/app/evals/scoring.py`, following waku's one-scorer rule
(`waku/ops/scoring.py:5-11`) so CI and any future UI scoreboard cannot disagree. **(c)** A judge
suite over recorded transcripts with an explicit threshold, plus a gate script modeled on
`waku/ops/release_gate.py:62-88` — deterministic must pass or exit 1, judge below threshold
exits 1, verdicts appended to a history file (`release_gate.py:57-59`). Carry over waku's two
hard-won judge details: the concurrency semaphore (`judge.py:35`) and retry-only-when-the-error-
is-transient (`judge.py:82-101`). Add it as a CI job after `backend` in
`.github/workflows/ci.yml`. **What it improves:** today `router.py:26-58` is a 33-line prompt
that decides whether a request provisions or destroys infrastructure, and **nothing** in 596
tests or 5 CI jobs detects a quality regression in it. This is the cheapest large risk reduction
available, and it runs entirely offline against recorded data — zero runtime, zero infrastructure
exposure.

### 4. Live flow diagram + a stalled-step tell in the artifact panel

Build `frontend/components/FlowDiagram.tsx` following the mechanic in
`waku/ops/static/js/diagram.js:105-131`: a static node/edge map keyed to step names, with each
node lighting up as its event arrives. The data already exists and is already ordered —
`timing.ORDER` (`timing.py:34-39`) defines the canonical 0-9 node sequence
(`router → cloudops_agent → policy_evaluation → planner → approval → execute → verify →
finalize → servicenow_update → notify`), `run_steps` rows carry real `started_at`/`ended_at`
(`timing.py:57-113`), and the SSE `step` event already fires per node
(`agents/events.py:198-199`). Add waku's hang tell alongside it: a step with `started_at` set
and `ended_at` NULL renders as stalled, mirroring `dashboard.py:224-227`'s "TURN NEVER
FINISHED". Build it inside the existing artifact-panel shell using only `design-reference`
tokens, per the pixel-exact mandate. **What it improves:** an operator watching a Production
apply currently reads across 8 tabs (`ArtifactPanel.tsx:108-115`) to answer "where is it?".
A lit diagram answers instantly, and the stall marker surfaces a wedged step immediately
instead of waiting up to 60 s for the reconciler sweep (`reconciler.py:34`) to notice. Pure
read-side; no backend contract changes.

### 5. An append-only per-org LLM cost ledger, with a budget that can halt

Add an `llm_usage` table (org_id, run_id, step, model, tokens_in, tokens_out, cost, ts) written
from `agents/llm.py:81-91` — the `_record` closure already holds `usage`, `model`, and timing at
the moment it posts a Langfuse generation. This is waku's `usage.jsonl` lesson made
multi-tenant: waku keeps a **permanent** ledger deliberately separate from resettable traces
because "tokens are the ground truth; dollar cost is derived" (`tracing.py:105-108`), whereas
AegisOps' cost exists *only* inside Langfuse (`langfuse_client.py:198-214`) and vanishes with a
key rotation, retention purge, or outage. Count the INV loop's spend too — waku's own regression
was uncounted sub-agent tokens silently understating every score (`experimental.py:19-22`).
Then close the gap **neither** system closes: a `max_cost_per_run` setting checked at the
existing safe boundaries — the INV loop's iteration edge (action 1) and
`exec_loop.py:279-282`'s step boundary — reporting a breach through the honest-partial path
already built for cancel (`_partial_outcome`, `exec_loop.py:334-354`). **Never** mid-apply: the
codebase's existing rule (`supervisor.py:76-83`) is that a running Terraform apply is not
force-killed, and a budget breach must respect it. **What it improves:** per-tenant chargeback
becomes possible, cost survives telemetry loss, and a runaway investigation stops on money
rather than on wall clock.
