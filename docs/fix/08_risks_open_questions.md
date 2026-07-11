# §7 — Risks, trade-offs, and open questions

[← back to FIX index](../../FIX.md)

## 7.1 Risks of the plan (and how to de-risk)

| Risk | Why it's real | Blast | De-risking |
|------|---------------|-------|-----------|
| **Multi-tenancy (S0) touches every endpoint** | `repo.get_default_org()` is called everywhere; changing principal→org resolution can break auth/session flows | high | Feature flag `AEGISOPS_TENANCY=strict|legacy`; migrate call sites behind it; parallel-run with the single-org default until `test_tenancy.py` is green; ship in one focused PR with a rollback path |
| **Redis Streams bus (B1) replaces the transport** | Streaming is the product's spine; a regression here breaks every run's UX | high | Keep the `Emitter` façade byte-identical so nodes don't change; flag `AEGISOPS_EVENT_BUS=memory|redis`; port the full `test_sse_contract.py` first; canary one worker |
| **Supervisor/reconciler (B2/B3) can double-drive a run** | Two things resuming the same run = double apply | medium | A1 idempotency wait-or-abort is a **prerequisite** (Phase 0 before Phase 2); reconciler only acts on expired-heartbeat runs; the checkpoint + idempotency make re-drive safe; test the exact race |
| **Real policy checks (U1) may fail plans that "worked" before** | Turning `True` into a real predicate surfaces genuine violations that were hidden | medium | Land predicates as **warnings** first (report fail but don't block), observe, then flip to blocking per policy; document each check's source-of-truth |
| **SRE real actions (U2) now mutate K8s** | A remediation that was a no-op will now actually roll back/scale | medium | Stays behind the existing approval gate; ship "proposed, not executed" first, then enable real actions per environment; never auto-remediate without approval |
| **Bounded planner (U6) changes the graph shape** | New control flow around the safety-critical path | high | Feature-flag; single-resource path stays the default; per-step `plan_guard`; one approval for the DAG; extensive DAG tests before enabling |
| **Latency changes (A3) skip `terraform init`** | Skipping init when "already initialized" could use stale providers | medium | Only skip when `.terraform/` + lockfile present **and** lockfile matches; fall back to full init on any mismatch; keep a force-init escape hatch |
| **Step-up re-auth (S1) adds friction** | A mandatory re-auth on every reveal could annoy users | low | Short re-auth window (≤120s) so a just-approved reveal doesn't re-prompt twice; clear UX copy; it's a deliberate security/UX trade the owner locked in |
| **Remote TF state migration (A3)** | Moving existing `local` state to remote can corrupt/lose state if done wrong | medium | Only for non-dev; documented `terraform state` migration with a backup; dev stays `local`; never migrate a workspace with an in-flight run |
| **Langfuse v2→v3 (O2)** | API/schema changes between majors | medium | Phase‑3, isolated; verify the v3 SDK shape at that time (flagged); keep v2 until v3 is proven in a branch |

## 7.2 What could regress (watch-list)
- **Approval-resume across the new bus** — the single most important flow; must survive worker restart *and* worker A→B handoff. Gate: the kill-mid-interrupt test.
- **Exactly-once SSE** — Redis stream ids must preserve the de-dup guarantee `_sse` gives today.
- **The safety guards** — `intent_guard`/`plan_guard`/state-isolation must stay green through the tenancy + bus + planner changes; they are the crown jewels. Every phase re-runs `test_safety_invariants.py` + `test_safety_live.py`.
- **Memory budget** — token-budgeting must not blow the context window on a long thread; test at 100+ turns.

## 7.3 Trade-offs the plan deliberately makes
- **Fix LangGraph, not replace with Temporal** — accepts building the supervisor/reconciler/bus ourselves in exchange for a far smaller blast radius and keeping the working durable-interrupt. Revisit if workflows lengthen (§7.4 Q4).
- **Keep Neo4j for now** — accepts a best-effort mirror's operational cost rather than a disruptive fold-into-Postgres mid-remediation. Decide with usage data at Phase 3.
- **Trim the model menu rather than implement all providers now** — honest UI over breadth; the interface makes later breadth cheap.
- **Policy predicates before OPA** — real checks fast, policy-as-code later; avoids a big dependency on the critical path early.

## 7.4 Open questions for the owner (needed before/at each phase)

1. **Step-up re-auth mechanism (Phase 1 gate):** password re-entry (simple, explicit) vs. silent OIDC `max_age=0` re-auth (smoother, more moving parts)? Both are supported by the existing Keycloak client. *Recommendation: password re-entry for the first cut.*
2. **Remote Terraform backend choice (Phase 1):** S3+DynamoDB (already env-plumbed) vs. Terraform Cloud vs. GCS+lock? Affects A3 and multi-cloud state locality. *Recommendation: S3+DynamoDB, matching the existing `TF_STATE_*` scaffolding.*
3. **Tenancy source of truth (Phase 1):** is org membership authoritative in Keycloak (a claim/group) or in the `users` table (seeded)? This decides how `principal→org` resolves. *Recommendation: Keycloak group/claim → mirrored into `users`.*
4. **Temporal (Phase 3):** do you foresee long-running, multi-stage DevOps/SRE pipelines (hours, many retries)? If yes, Temporal becomes worth the migration; if no, the fixed LangGraph harness suffices. *Need your read on the product direction.*
5. **Neo4j (Phase 3):** invest in the cross-run/incident-graph features (a real differentiator) or fold provenance into Postgres and drop it? *Need a product-value call.*
6. **Cost estimation:** the UI implies "$/mo within guardrail" but no code computes cost. Build real estimation (Infracost/provider pricing feeding the policy check + approval card) or remove the implication? *Not in the P-list; your call on scope.*
7. **SRE auto-remediation posture:** should any remediation ever run without a human approval (e.g. a pre-approved "restart on crashloop"), or is every mutation always gated? *Governance stance needed before U2 enables real actions.*
8. **Multi-provider ambition (P10/U3):** is genuine model choice (Claude/GPT/Gemini) a near-term product requirement, or is Gemini-only fine with the interface in place for later? *Affects how much of U3 to build now.*

## 7.5 What this plan explicitly does NOT do
- It does not modify any code (this is the blueprint; implementation is the staged passes gated by [07 roadmap](07_roadmap.md)).
- It does not weaken any existing safety guard — every change hardens or preserves `intent_guard`/`plan_guard`/state-isolation/approval.
- It does not rewrite the pixel-exact UI or the design-token system (out of scope; the analysis flagged the inline-style maintenance debt as "reconsider," not "rewrite").
- It does not commit to Temporal, an OPA rollout, or dropping Neo4j — those are Phase‑3 **decisions** with the data to make them, surfaced above.
