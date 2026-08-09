# Redesign/ — AegisOps Target Architecture & Migration Blueprint

Produced 2026-08-09 against HEAD `a974290` (branch `feature/cloudops-v3`). Supersedes the
`Brainstorming/` suite (2026-08-03), whose provider-layer and engine designs it carries forward
and whose 13 internal inconsistencies it resolves (tracked as I1–I13 inside 04/05/06/07).
Reference architectures studied at source level: Waku (local), Pi `earendil-works/pi@936aff0`,
Hermes `NousResearch/hermes-agent@3f83297`, OpenClaw `v2026.8.1`.

| Doc | Contract |
|---|---|
| [00-Redesign-Mandate](00-Redesign-Mandate.md) | North star, harness-first principle, boundary classes, constitution, success criteria |
| [01-Current-State-Architecture](01-Current-State-Architecture.md) | Audited current state, defect register D1–F-22, tech roles |
| [02-Redesigned-Architecture](02-Redesigned-Architecture.md) | Plane model, target diagram, packs, subagents, multi-cloud, user-diagram review |
| [03-Platform-Features](03-Platform-Features.md) | Objective taxonomy, workflow classes, 3×10×9 coverage matrix, DevOps/SRE catalogs |
| [04-Agent-Harness-Specification](04-Agent-Harness-Specification.md) | Loop laws, kernel, provider layer, budgets, hooks, policy/approvals, verification, eval plane |
| [05-Tool-and-Agent-Contracts](05-Tool-and-Agent-Contracts.md) | ToolDef, effects vs policies, middleware, trust-boundary payloads, evidence contracts |
| [06-Memory-Context-and-Execution](06-Memory-Context-and-Execution.md) | Memory tiers/lifecycle, context engine, compaction, run log, status machine, background execution |
| [07-Migration-and-Implementation-Plan](07-Migration-and-Implementation-Plan.md) | Phases 0–5, decision gates, risk register, removal schedule |
| [08-Architecture-Decision-Records](08-Architecture-Decision-Records.md) | ADR-01…18 incl. the full LangGraph evaluation; human sign-off list |
| [09-Architecture-Readiness-and-Traceability](09-Architecture-Readiness-and-Traceability.md) | Readiness gate: traceability matrix R1–R34, PLAN MODE resolution, parity matrix, security readiness, verdict: **READY** |
| [10-Behavioral-Acceptance-Matrix](10-Behavioral-Acceptance-Matrix.md) | Executable scenarios A–W + intelligence proofs IP-1..4 + phase gate index |
| [diagrams/](diagrams/) | Presentation-ready SVG/PNG renders (Mermaid sources in `diagrams/src/`) |

---

# Final Audit

## 1. Top current architectural problems (evidence: 01)

1. Zero iterative reasoning anywhere — single-pass DAG, empty director seat, replanner returns `None`.
2. LLM layer validates but doesn't dispatch — one Gemini singleton; second provider = rewrite.
3. Zero native tool calling — prompt-and-parse JSON; `tools=` never populated.
4. Fat domain agents (`cloudops.py` 1,531 LOC) own control flow that belongs to a harness.
5. No enforceable budgets; cost data lives only in a resettable trace store; the one guardrail env var is unreachable.
6. Memory has no tiers above the transcript, no gate, no consolidation, no contradiction handling.
7. No behavioral eval gate — 741 tests, none catch a routing-quality regression.
8. Verification is tool-success-shaped and AWS-skewed; Azure/GCP resources can be created but barely seen.
9. Multi-tenancy stops at Postgres — one global long-lived cloud credential set for all tenants.
10. Governance posture drifts silently via `.env` (four-eyes off in the shipped install).

## 2. Target architectural principles (02 §1)

Harness-first · split-trust preserved · thin packs, one kernel · cloud-neutral core with parity
gates · policy as code per action · budgets inside the loop · errors are data · evidence over
claims · run log as source of truth · minimal framework surface.

## 3–12. Current vs target

| Axis | Current (`a974290`) | Target |
|---|---|---|
| **Architecture** | FastAPI + 12-node LangGraph spine + fat agents | Plane model: control → harness(kernel+packs) → provider layer → governed engine; graph reduced to interrupt/checkpoint substrate |
| **Agent behavior** | route → plan → approve → execute → verify, single pass; failure = halt + report | observe→reason→plan→policy-check→act→observe→verify loop; diagnose/re-plan/retry/ask/delegate; failed calls change the next action |
| **Memory** | transcript + human-set `user_memory`; unconditional retrieval | 5 tiers w/ provenance/confidence/lifecycle; gated retrieval; consolidation→human-accepted proposals; supersede-not-coexist contradictions; governed skills |
| **Tools** | typed Python clients called by name from agent code | typed registry (schema/effect/risk/verify), native FC, policy middleware, progressive disclosure, frozen per run |
| **Model strategy** | 1 provider, 3 ids, contextvar, no params | 6 adapter families, purpose routing, capability registry, eval-gated bindings, two-stage failover, ledger + budgets |
| **Execution** | one HTTP-driven pass; exec_loop sequential, off-by-default (on in this install) | durable Task/Run/Step + run-log replay; waves; saga rollback; day-2 verbs; change windows; worker role |
| **Failure recovery** | honest halt + partial (good) but no retry/diagnosis | taxonomy→action mapping, bounded retries, deviation proposals, compensation chains, stuck detection, grace-call partials |
| **Security** | strict tenancy/RBAC/four-eyes(default)/redaction; global cloud creds; unauth /metrics; drifting .env | same constitution + hardline deny tier, contextvar approval state, import-frozen flags, args-hash-bound approvals, ESTOP, per-org brokered creds, stamped governance flags |
| **Observability** | deep traces; 11 metrics (1 dead, 4 charted); cost in Langfuse only | + run-log projections, ledger dashboards, served-by badges, fallback visibility, flow console, all metrics charted, /metrics authed |
| **Evaluation** | none behavioral | dataset + judge + regression gate in CI; 9 evaluated dimensions; gates prompts/bindings/packs/inversion; offline arena |

## 13. AWS / Azure / GCP capability parity

Today: write catalog 7/7/6 but reads 6/3/2 services; verify AWS-only; drift aws-ec2-only.
Target: 10 service families × 9 verbs per cloud (03 §3), **parity gate in CI** — a family ships
only when all three clouds expose the same verb set. Phase: P5.1.

## 14. Migration priority (07)

P0 protect & repair (eval gate, ledger, defects, security hygiene) → P1 provider layer →
P2 kernel on read paths (INV loop, gate, run log) → P3 engine (waves/saga/day-2/K8s/windows) →
P4 harness-first inversion (dark-launched, eval-parity-gated) → P5 parity/credentials/channels/
incident pipeline. Each phase independently valuable; P4 is the point of no return and ships
behind a flag.

## 15. Must be removed (07 removal schedule)

Gemini singleton + validate-only seam · hardcoded model menu · `applying` phantom status · dead
code register (D7) · `_todo` blanket policy rows · fat domain agents · 12-node graph as spine ·
global credential set · LangChain direct import · secrets/state residue in the working tree.

## 16. Must be preserved (00 §7, 01 §5)

Terraform-only catalog mutation · durable approval interrupt · plan_guard at the choke-point ·
strict tenancy/RBAC/four-eyes · per-step idempotency · boundary-only cancel · honest partials ·
redaction on egress · trace==run · immutable approvals · investigation registry's read-only
boundary · TF state isolation · supervisor/reconciler recovery · GW-1 transport seam + click-time
re-checks · the 70/30 transcript budgeter · SSE replay/cursor mechanics.

## 17. Should be refactored (not replaced)

`exec_loop` → engine (invariants carried verbatim) · investigation registry → INV mode under the
kernel · `agents/llm.py` → service shim → deleted · memory.py recipes → context engine ·
Neo4j world model → interface (fold-in expected) · Langfuse cost → ledger · reconciler → run-log
replay resume · SRE PromQL → per-service templates.

## 18. Highest-risk migrations (07, ranked)

1. P4.3 loop-as-spine (mitigation: dark launch, eval parity, flag rollback)
2. P3.1/3.2 exec_loop→engine+saga (keep/generalize/new table as contract)
3. P5.3 credential brokering (dual-path, never silent fallback)
4. P1.3 dispatch re-route (rule zero: gate green first)
5. P2.5/P3.6 run_events + status machine (kill -9 drills before authority shifts)

## 19. Decisions requiring human approval (08 consolidated)

Harness-first inversion (pre-P4) · LangGraph end-state (post-P4 gate) · Neo4j fold-in (P5 gate) ·
credential brokering design (pre-P5.3) · four-eyes re-enable or signed waiver (**now**) ·
AUTONOMOUS mode + pre-approved verb lists (pre-P4.5/P5.4) · Temporal (standing gate, default no) ·
max_steps raise + concurrency caps (pre-P3.9).
