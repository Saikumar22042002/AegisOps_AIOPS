# 10 — "Seamless like Claude Code / ChatGPT / Antigravity" — gap analysis

[← back to index](../../ANALYSIS.md)

The bar: a continuous, low-latency, streaming, multi-step-autonomous agent that recovers from errors, keeps context, and makes its state legible — the feel of Claude Code, ChatGPT, or an IDE agent like Antigravity. Here's where AegisOps stands against that bar, grounded in the code, and what to change.

## 10.1 Where it already meets the bar
- **Token/step streaming** is real and smooth (`Emitter.token`/`step`, `lib/store.ts` reducer, live timeline). The CRLF frame fix means it actually renders.
- **A visible plan-then-act loop with human approval** is genuinely agentic and safer than most chat products — the interrupt is durable.
- **Per-message run binding** (each assistant message pins its own artifact panel/run) is a nice continuity touch most chat UIs lack.
- **Resource memory** (inventory + graph) gives real day-2 continuity: "add ports to test-vm" in a new session works.
- **Honest failure messages** (`provider_errors`) with what/why/next-step beat raw stack traces.

## 10.2 Where it falls short (and why, from the code)

**Continuity of conversation.** Claude Code/ChatGPT keep the whole thread in context; AegisOps keeps the current session only, and lossily once long (see [06 §6.2](06_memory.md) — older turns become 160-char digests, assistant turns drop out). There is **no cross-session memory** and **no user profile/preferences memory**. Ask "what did we decide last week?" and it has nothing. The router only sees the last 8 turns, so mid-thread references don't resolve.

**Latency.** Every turn: Gemini classify (router) → often a second Gemini extract (cloudops) → SDK availability → `terraform init` (run **unconditionally** every request, `cloudops_plan`) → `terraform plan`. PROGRESS's own audit measured EC2 `init` ~19s + `plan` ~21s warm. So a provisioning turn is tens of seconds before the approval card appears — far from the sub-second feel of a chat product. Init-skip-when-initialized and a plugin cache would cut ~13–19s; the OneDrive bind-mount amplifies TF's many-small-file I/O.

**Multi-step autonomy.** The graph is a **fixed, single-pass** pipeline: one classify → one plan → one approval → one apply. It cannot decompose "stand up a 3-tier app" into multiple provisioning steps, chain resources (create VPC then EC2 in it), or self-correct a failed plan by trying an alternative. Claude Code loops tools until done; AegisOps runs the graph once and stops. There is no planner that emits a multi-resource DAG.

**Error recovery.** Failures are explained well but **not auto-recovered** — no retry-with-fix, no "the region was bad, let me try us-west-2." The user must re-issue. Stream truncation is the one place with real resilience (`llm.stream_answer` retries).

**Undo.** There is destroy (day-2), but no "undo my last apply" affordance and no rollback of a partial apply (P14 leaves orphans). A seamless agent would offer "revert this change."

**Clarity of state.** Good live timeline, but the **Traces tab is fake** (P9), **policy checks are fake** (P8), and **SRE remediation lies about success** (P7) — these erode trust in the state the UI shows. The timeline is accurate; several sibling tabs are not.

**Interactive input mid-run.** The `POST /runs/{id}/input` endpoint exists but is **dead** (P13) — so the "answer a password prompt during a run" affordance doesn't work. A seamless agent handles mid-run prompts.

## 10.3 Concrete, prioritized changes to reach the bar

**Tier A — continuity & latency (biggest felt improvement):**
1. **Skip `terraform init` when initialized** (`.terraform/` + lockfile present) and set `TF_PLUGIN_CACHE_DIR` on a named volume; move TF state off the OneDrive bind-mount. Saves ~13–19s/turn. *(Architecture supports this — it's a guard in `cloudops_plan`.)*
2. **Token-budgeted memory with summarization**: replace char-truncation with a token budget + a rolling LLM summary of dropped turns, and give the router more than 8 turns (or a retrieval over the transcript). *(Additive to `agents/memory.py`.)*
3. **Cross-session + user memory**: a per-user "what you've built / preferences" store surfaced into `general`/router. *(New, but the inventory already gives cross-session resource memory to build on.)*

**Tier B — autonomy & recovery:**
4. **Multi-step planner node**: let CloudOps emit a small DAG of resources (e.g. VPC→subnets→EC2) approved as one plan, applied in order. *(Structural — the current single-pass graph can't express this; needs a plan/loop sub-graph or a Temporal-style workflow.)*
5. **Auto-recovery loop**: on a classified provider error with an obvious fix (bad region/zone, name taken), offer/auto-apply the fix and re-plan, gated by approval. *(`provider_errors` already classifies; add a retry edge.)*
6. **Undo/rollback**: record the pre-change state ref and offer "revert" (destroy the just-created resource / re-apply the previous plan). *(Inventory + per-resource state make this feasible.)*

**Tier C — trust & polish (cheap, high trust ROI):**
7. Make the **Traces tab real** (P9), **policy checks real** (P8), and **SRE remediation real or clearly labeled** (P7).
8. **Wire mid-run interactive input** (P13) or remove the affordance.
9. **Evict SSE channels + move the bus to Redis** (P4) so reconnect/latency stay smooth under scale.

## 10.4 Does the current architecture support the bar?

**Mostly yes for Tier A/C, structurally no for Tier B #4.** Continuity, latency, trust, and recovery are all additive changes to existing nodes/stores. **True multi-step autonomy (chained resources, self-correcting loops) is the one place that needs a structural change** — the graph is a fixed single-pass state machine, and LangGraph *can* express loops/sub-graphs but the current design deliberately doesn't. That's the fork in the road: keep the safe single-pass model (fine for "provision one resource with approval") or invest in a planner/loop sub-graph (or Temporal) to reach IDE-agent-grade autonomy. Given the safety posture, a **bounded, approval-gated multi-step planner** (each step still hits the human gate, or one gate for the whole DAG) is the right target — not an unbounded autonomous loop.
