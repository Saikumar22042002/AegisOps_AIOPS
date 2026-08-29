# AegisOps — Forensic Current-State Audit

**Date:** 2026-08-16 · **Branch:** `feature/cloudops-v3` · **Method:** live runtime testing against the running stack (sandbox AWS account `379620500929`, real terraform applies, SSE captures at container `/tmp/e2e/*.json`) + store-level inspection (Postgres, Neo4j, Redis) + code tracing. Every status below is backed by observed runtime behavior or a store query, not by the existence of a file, table, or UI element.

**Statuses:** `IMPLEMENTED` (proven working on the live path) · `PARTIAL` (works with proven defects/limits) · `BYPASSED` (code exists, live path routes around it) · `DARK` (code exists behind a default-off flag; proven working when flipped, or noted otherwise) · `UNUSED` (wired but nothing feeds it) · `BROKEN` (proven wrong behavior) · `MISSING` (does not exist).

Companion docs: `INTELLIGENCE_LAYER_AUDIT.md` (data lineage of resource questions), `CONTEXT_GRAPH_MODEL.md` (Neo4j reality), `RETRIEVAL_FLOW.md` (context assembly reality), `AGENT_LOOP.md` (governed executive loop proof).

---

## 1. The pipeline that actually runs

For a chat message, the ONE live execution path is:

```
Browser (Next.js 14, SSE)
  → POST /chat                                    app/api/chat.py:421
  → require_initiator (Keycloak session cookie)   app/security/deps.py (COOKIE_NAME="aegis_session", Redis-backed)
  → prepare_run → runs row + Redis event channel
  → LangGraph graph (checkpointed in PG)          app/agents/graph.py:83-108
      START → router → {cloudops_plan | devops_plan | sre_analyze | knowledge | general}
            → approval (HITL interrupt) → execute → verify → finalize
            → servicenow_update → notify → END
  → SSE frames (run/step/console/token/interrupt/served_by/confidentiality/analysis/done)
```

The P2 "harness", P3 "engine", and P4 "capability packs" are **not** on this path (see §4). There is no second pipeline in production.

## 2. Component status (evidence-backed)

| Component | Status | Evidence (2026-08-16) |
|---|---|---|
| Frontend (Next.js 14, SSE) | IMPLEMENTED | serves on :3000; P1–P5 markers in built chunks; live SSE consumption |
| API surface (FastAPI) | IMPLEMENTED | routes in `app/api/*` (no prefix except `/auth`, `/gateways`); all exercised live |
| Auth / RBAC | IMPLEMENTED | Keycloak password grant; session in Redis (`sess:*`); proven: 401 unauth, 403 read-only initiate, 403 non-approver approve, 404 cross-org (non-enumerating), org-scoped queries throughout |
| Orchestrator (LangGraph) | IMPLEMENTED | `graph.py` wiring above; durable checkpointing REAL: `checkpoints`=183, `checkpoint_writes`=870, `checkpoint_blobs`=205 rows |
| Router | PARTIAL | LLM classify (`purpose=router`, gemini-3.5-flash) + `normalize_classification` + deterministic guards. Defects: JSON-flake fallback to `general` at 30% conf (run 2d2b7fc5); "who approved the run" → intent `get_run_approval_details` routed to **devops** which demands GITHUB_TOKEN (run b05e7b50); routine 50% confidences |
| Context assembly | PARTIAL | `app/agents/memory.py::build_context` — standing user memory + positional recall + top-3 session-scoped pgvector hits + char-budgeted transcript. **Consumed only by router/general/knowledge. CloudOps uses none of it** (grep: no `build_context` in cloudops.py) |
| Retrieval (documents/RAG) | UNUSED | `rag/retriever.retrieve` wired into knowledge/SRE/api, but `documents`=0, `document_chunks`=0 rows — the corpus has never been fed; every "knowledge" answer is pure LLM + transcript |
| Retrieval (message vectors) | IMPLEMENTED (narrow) | `messages.embedding` populated (537/1845 rows; 214 `embedding` rows in llm_usage); `memory.retrieve()` cosine top-3, session-scoped only |
| Planner | IMPLEMENTED (deterministic) / model binding COSMETIC | Planning = template + `terraform plan` + policy checks (real). The governed `planner` LLM purpose has **never been invoked**: `llm_usage` purposes = embedding/router/classify/extract/general/sre.triage/gate_smoke only |
| Agent Loop (`exec_loop`) | DARK — proven working | `aegisops_exec_loop` default `off` (settings.py:47); entered only from `cloudops.py:618` when dependency closure yields a DAG. Probe with flag on: run `bed04e5a` → `workflow: governed-exec-loop`, per-step plans, one whole-DAG approval. See `AGENT_LOOP.md` |
| Harness (P2, `app/harness`) | DARK / sliver live | `aegisops_harness_read_paths` default off. Live uses: `sre.py:89` (investigation inventory), `artifacts.py:167` (run_log reasoning tab). `retrieval_gate`/`consolidation` exist ONLY inside `harness/memory.py` — never executed |
| Engine (P3, `app/engine`) | DARK — zero callers | no imports of `app/engine` outside itself+harness; `tasks` table 0 rows; posture `durable_engine: off` |
| Capability packs (P4) | IMPLEMENTED (read surface) | `/capabilities` returns 5 packs, `packs_enabled` reflects flag; objective model maps intent→template; packs are metadata/templates, not a separate execution path |
| CloudOps agent | PARTIAL | Create/destroy/read proven end-to-end incl. approval+apply+verify. Proven defects: §3 |
| DevOps agent | PARTIAL (gated) | requires GITHUB_TOKEN; also wrongly owns run-approval-audit questions |
| SRE agent | PARTIAL (lightly exercised) | `sre_analyze` + harness investigation; 1 `sre.triage` llm_usage row ever |
| Terraform toolchain | IMPLEMENTED with 1 BROKEN template | real init/plan/apply, per-resource state workspaces (`res-<name>`), plan guard (create-never-destroys) proven live; policy checks (IMDSv2, encryption, cost) real. **BROKEN:** `aws.vpc` AZ selection has no `opt-in-status` filter → in Local-Zone-enabled accounts every subnet lands in `us-east-1-atl/bos/chi` → NAT GW + instance launches fail (runs dda2ecd9, af4442d1, 3426df8c) |
| Cloud SDK discovery | IMPLEMENTED | live boto3 / azure / gcp reads on the read path; correct live counts every probe |
| Verification | IMPLEMENTED | `finalize.py:64 verify()` — bounded (`_VERIFY_TIMEOUT_S`) real cloud reconciliation post-apply; evidence node into context graph |
| Memory (standing/user) | UNUSED | `user_memories`=0, `memory_items`=0 rows; `/memory` API works, nothing writes it in practice |
| Response composition | IMPLEMENTED | finalize + cards; confidentiality classifier on every answer; secrets via one-time Redis reveal (`reveal:<uuid>:private_key_pem`), `repr`-safe grants |
| Observability | PARTIAL | one Langfuse generation per LLM call (`llm.<purpose>`, service.py); OTel spans; Prometheus metrics; run timeline API real. **Defect:** Langfuse init keys belong to project "AegisOps" ≠ expected "aegisops" → traces misfiled, dashboard empty (`langfuse.wrong_project` warning at boot) |
| Audit log table | UNUSED | `audit_log` = 0 rows, ever. Approvals table + graph carry the only decision audit |
| run_events table | UNUSED | 0 rows (migration 0012 artifact); run events live in Redis streams (`run:<uuid>:events`) + `run_steps` (151 rows) |

## 3. Proven live defects (this audit's runs)

> **REMEDIATED 2026-08-17:** items 1, 2, 3, 4 (binding + default-VPC), 5 (ghost sweep),
> 6 (cross-cloud), 7 (partial — parameter continuity), 8 (approval-audit routing) and the
> history/provenance gap are FIXED with live evidence — see
> `PROD_CORRECTNESS_REMEDIATION.md`. Item 9 (router JSON flakes) remains open (model
> behavior; the safe fallback stands). The immutable `resource_revisions` journal
> (migration 0016) now answers what-changed/when/who/previous-config deterministically.

1. **Failed applies orphan real infrastructure and wedge the platform.** `audit-vpc` apply failed on NAT GW (run dda2ecd9) → VPC existed in AWS, absent from inventory (only successful applies are inventoried), retry-create blocked by the (correct) plan guard, destroy refused ("I only tear down infrastructure I created"). No recovery path exists inside the product. P0.
2. **Day-2 verb inversion: port removal is impossible.** "Remove/close inbound port 8501" planned as "open inbound TCP [8501]" both times (runs 7176806f, c31d3250); since the port was open, terraform planned 0 changes and the run reported **applied: true**. Live SG kept 8501 open. Compound defect: modify-extraction ignores the remove verb + no zero-change guard + success card re-emitted. P0.
3. **`aws.vpc` template Local-Zone AZ selection** (above). P0 for any opt-in-zone account.
4. **VPC dependency binding never reads the message.** "in the audit-net VPC" (stated twice verbatim) still produced the disambiguation menu; the menu is closed (inventoried VPCs or "new"), free text ("use the account default VPC") re-loops, and the account's default VPC is not an option. Explicit `subnet-…` ID is the only escape. P1.
5. **Stale inventory contaminates operations and answers.** 13 of 16 "active" rows are resources from dead sandbox accounts; they appear in answers as fact and in dependency menus as placement options (choosing one would fail). No reconcile-on-read for listings (single-resource reads DO reconcile). P1.
6. **Cross-cloud contamination:** "What are the resources I have created in **AWS**?" returned GCP rows (gcp-vm-test, my-test-vm, mybucket-…) — the broad-inventory path ignores the cloud constraint in the question (run 99454ced). P1.
7. **Parameter memory does not survive a failed run:** the t2.micro retry forgot OS/key/access/VPC from the immediately-preceding attempt in the same session (run 6e931805). P2.
8. **Approval-audit questions unanswerable conversationally** despite full data in `approvals` (actor_user/actor_role/ts) and graph (`Approval-[:DECIDED_BY]->Human`). P2.
9. **Router structured-output flakes** → silent fallback to general ("I do not have access to your live AWS environment" for an EC2 count question, run 2d2b7fc5). P2.

## 4. Source of truth — model vs reality

| Store | Intended role | Actual observed role |
|---|---|---|
| Live cloud | current infra state | Read on: single-resource reads (reconcile), discovery summaries, post-apply verify. **NOT read for inventory listings** → stale rows presented as "active". Live state does NOT override stale memory in list answers |
| PostgreSQL | app/resource/run/audit records | REAL system of record: runs(23 recent)/run_steps(151)/approvals(8, immutable)/messages(1845, 537 embedded)/resources(36, **upsert = history overwritten**, single run_id pointer = last-touch)/LangGraph checkpoints. `audit_log`, `run_events`, `memory_items`, `documents` are empty artifacts |
| Neo4j | relationships/provenance/temporal | WRITTEN on every run+mutation (rich: Run/Session/Resource/Approval/Human/Step/Evidence/Tool + CREATED/PROVISIONED/DEPENDS_ON/DECIDED_BY/REQUIRED_APPROVAL). READ on exactly 3 paths: resource-card provenance line (cloudops.py:1078), destroy impact check (`world_model.impact_of`, cloudops.py:1107), investigations. **Never used for question answering/retrieval** |
| pgvector | semantic retrieval | messages.embedding live (session-scoped k=3 recall); document_chunks empty; memory_items empty |
| Redis | cache/events/ephemeral | run event streams (`run:<id>:events`), web sessions, idempotency keys, one-time secret reveals, rate limits — as designed |
| Conversation store | raw conversation | `messages` table + digest/transcript building — as designed |
| Langfuse/OTel | observability only | as designed, but Langfuse project mismatch misfiles traces |

**Verdict:** the intended layering exists, but (a) inventory listings trust PG over live cloud, (b) Neo4j is a write-mostly provenance archive, (c) pgvector's document half is empty. "Live cloud must override stale memory" is violated on the listing path.

## 5. Live test evidence log (all runs 2026-08-16, session e4273347)

| Step | Run | Result |
|---|---|---|
| VPC audit-vpc (NAT) create+approve | dda2ecd9 | apply_failed (NAT in Local Zone) → **orphan in cloud** |
| Retry create no-NAT same name | 13dd14d5 | blocked_by_guard (create would destroy nat EIP) — guard correct |
| Destroy audit-vpc | aba2baad | refused — not in inventory → **wedged** |
| VPC audit-net (no NAT) | e066b76d | applied: vpc-04c5a318f0baf0e3f, 6 subnets — all Local Zones |
| EC2 My-source in audit-net | af4442d1 | apply_failed (Unsupported — Local Zone) |
| EC2 retry t2.micro | 3426df8c | apply_failed (same) |
| EC2 MySource, explicit default-VPC subnet | 53b76da7 | applied: i-01b63e5d465382213 |
| Open port 8501 | 724e400b | applied, live SG [(8501,8501)] ✓ |
| "Remove port 8501" | 7176806f | **applied:true, no-op — port still open** ✗ |
| "Close port 8501" retry | c31d3250 | planned "open [8501]" again → rejected by tester |
| Agent Loop probe (flag on) | bed04e5a | governed-exec-loop DAG (vpc→ec2), one approval → rejected by tester, no mutation |
| Core queries q1–q7 | 99454ced…7e1cc90b | see INTELLIGENCE_LAYER_AUDIT.md |

Residue left in sandbox (auto-expires): audit-vpc orphan, audit-net (Local-Zone VPC), MySource (running, port 8501 open), partial My-source SG/key pair.

## 6. Top 10 production blockers (ranked)

1. Silent no-op mutations reported as success (port removal) — trust-breaking; users believe a change happened.
2. Failed-apply orphans with no in-product recovery (retry blocked, destroy refused, invisible to inventory).
3. `aws.vpc` Local-Zone AZ selection — every VPC broken in opt-in accounts.
4. Inventory listings never reconciled against live cloud — stale "active" ghosts stated as fact and offered as placement targets.
5. Cross-cloud leakage in cloud-scoped questions.
6. No temporal/history answers despite the data existing (runs, approvals, accreting graph edges) — "when/what changed/previous config/who approved" all fail.
7. Resource history overwritten in PG (upsert; run_id = last touch); graph mislabels every modify as CREATED; before/after never stored as a diff.
8. Dependency/parameter binding gaps (VPC by name ignored; closed menus; default VPC unreachable; params lost after failure).
9. Governed model bindings partially cosmetic (`planner`, `judge`, `loop.main` never invoked) + Langfuse project mismatch = observability blind spots.
10. Intelligence-layer flagship components (engine, harness read paths, retrieval gate, consolidation, RAG corpus, standing memory) dark or unfed — the "AI-native memory" story currently rests on transcript + k=3 session recall only.

## 7. Required fixes

**P0 (correctness/safety):**
- Modify path: honor remove/close verbs (day-2 extractor must produce the post-change port set); add a zero-change guard — a modify whose plan is all no-ops must say "nothing would change" and never report "applied".
- Failed-apply recovery: on apply failure, record a `partial` inventory row (state exists in the workspace) so destroy/retry can operate on it; or auto-`terraform destroy` the partial state after user confirmation.
- `aws.vpc`: filter AZs to `opt-in-status=opt-in-not-required` (or offer AZ inputs).
- Inventory listing answers: reconcile per-cloud against live SDK (the reconcile primitive already exists for single resources) and mark/exclude ghosts; filter by the cloud named in the question.

**P1 (trust/answerability):**
- Route `get_run_approval_details` to a deterministic PG/graph read (data already complete).
- Temporal answers: expose runs/approvals/graph history for "what changed / when / previous config" (before/after = previous inventory attributes snapshot per mutation run — start persisting an immutable `resource_revisions` append table or reuse the accreting graph edges with a proper CHANGED label + payload).
- Dependency binding: match named VPC/parents from the message against inventory before menuing; include the default VPC as a first-class option; accept free-text menu answers.
- Fix Langfuse project provisioning (keys ↔ project name).

Graphiti's role in the P1 temporal story is assessed in `CONTEXT_GRAPH_MODEL.md` §5 — recommendation: coexist (separate Neo4j database, `add_triplet` for audit facts), not replace.
