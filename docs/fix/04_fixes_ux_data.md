# §3c — Fixes: UX / rendering / seamless feel · Data layer

[← back to FIX index](../../FIX.md) · Sizing **S/M/L**, blast low/med/high.

---

## U. UX / rendering / seamless feel

Grounded in [ANALYSIS §10 gap analysis](../analysis/10_gap_analysis.md). The streaming/rendering basics are already good; these close the "seamless agent" gap and remove the theater that erodes trust.

### U1 — Governance honesty: real policy checks · **P8** · M · blast: medium
**Now:** `agents/templates.py` policy fns return `_ck(name, True)` regardless of inputs/plan — the approver sees "6/6 passed" that were never evaluated.
**Change:** make each check a **real predicate** over `validated` inputs and the `terraform show -json` plan. Two tiers:
- **Tier 1 (cheap, now):** rewrite each `_*_policy` to assert against actual values — e.g. `_ec2_policy` checks the plan's `root_block_device.encrypted == true` and `metadata_options.http_tokens == "required"` from the plan JSON, not a literal `True`. `_s3_policy` reads the plan's public-access-block + SSE config. Fail = a real failed check the approver sees.
- **Tier 2 (Phase‑3 differentiator):** integrate **OPA/Conftest** against the plan JSON for org-authored policy-as-code. *(Flag: confirm OPA vs. a native predicate library at that decision.)*
**Verify:** a plan with encryption disabled → the encryption check reports **failed**; `tests/test_templates.py` asserts checks reflect the plan.

### U2 — SRE remediation: real or honestly labeled · **P7** · M · blast: medium
**Now:** `agents/sre.py:146 sre_execute` returns `{"applied":True}` after only `list_deployments`; `agents/sre.py:53` hardcodes `recent_deploy:True`.
**Change:**
- **Signals:** replace the hardcoded `recent_deploy` with a real Prometheus query (deploy annotation / recent-change metric) in `_collect_telemetry`.
- **Actions:** implement the real K8s operations behind the decision matrix — rollback (`rollout undo` / patch to prior revision), scale_out (patch replicas), restart (`rollout restart` annotation) via `tools/kubernetes.py` (extend it). If K8s isn't configured, return an explicit **"proposed, not executed"** outcome — never `applied:True`.
**Blast radius:** `agents/sre.py` + `tools/kubernetes.py`; behind the existing approval gate.
**Verify:** `tests/` — with a fake K8s, a `restart` decision issues the real patch and reports it; without K8s, reports "proposed, not executed" (not success).

### U3 — Real model selection (LLMProvider) · **P10** · M · blast: medium
**Now:** `body.model` never read; global Gemini singleton; UI lists Claude/GPT/Llama.
**Change:** introduce an `LLMProvider` protocol (`classify_json`/`generate`/`astream`/`aembed`) with a `GeminiProvider` implementation; a `get_provider(model_id)` factory; thread `body.model` from `/chat` through the run into `get_provider`. **Trim the UI menu** (`frontend/lib/data.ts`) to what's implemented (Gemini variants) until other providers land; keep the interface so adding one is a new class, not a rewrite. Also fix the sync-in-constructor model resolve (P18) as part of this.
**Blast radius:** `integrations/gemini.py` → a new `integrations/llm/` package; `agents/llm.py` calls the provider; contained by the façade.
**Verify:** `body.model` selects the provider; a request with an unknown model → clear error; the menu shows only real options.

### U4 — "Ask which cloud" reachable · **P11** · S · blast: low
**Now:** `resolve_cloud` falls back to the UI selector, which defaults `AWS` (`api/chat.py:40`, `frontend/lib/store.ts:137`).
**Change:** add an **"Auto / ask me"** cloud option (maps to `cloud=null`) and make it the store default; `resolve_cloud` only uses the selector as a hint when the message names no cloud **and** the user pinned a specific cloud. Ambiguous + Auto → the clarifying question fires (the behavior the analysis showed is currently unreachable).
**Verify:** with Auto selected, "provision a virtual machine" → asks which cloud; with AWS pinned, → AWS.

### U5 — Mid-run interactive input (wire the dead path) · **P13** · S · blast: low
**Now:** `POST /runs/{id}/input` rpushes to `runinput:<run_id>` (`api/chat.py:248`) with **no consumer**.
**Change:** either (a) wire it to `tools/console.py:CommandConsole.send_input` for the run's live process (via the supervisor from B2), so a tool can prompt mid-run; or (b) if no near-term flow needs it, **remove** the endpoint and the Redis key to kill the dead code. Recommend (a) only if a real interactive tool flow is planned; else (b).
**Verify:** if wired — an interactive prompt is answered end-to-end; if removed — no `runinput:` references remain.

### U6 — The Governed Executive Loop (rewritten per decision 8, final) · **ANALYSIS §10 §B4** · L · blast: high
**Now:** the graph is single-pass — one resource per run; can't chain VPC→subnet→EC2 or self-correct.
**Change (supersedes the earlier "bounded planner sub-graph"):** an LLM loop **at the planning level**, on LangGraph/`create_agent` primitives (see [01 §2.7](01_harness.md#27-split-trust-and-the-governed-executive-loop-stage-a-amendment--decisions-78-final)):
- **Goal → DAG:** the loop drafts a **goal DAG** — each node an approved module + params, or a read-only verification step.
- **ONE approval for the whole DAG:** the approval artifact is the plan itself (ordered steps, per-step plan summaries + policy checks, cost signal). New UI card; live per-step progress in the timeline.
- **Deterministic execution:** code walks the DAG; every step goes through `execute_governed_step(cloud, resource, action, params)` — the single mutating tool whose interior is the full governed pipeline (validate → plan → per-step `plan_guard` → apply in isolated state → verify → record), with per-step idempotency.
- **Observation feedback:** structured observations (new VPC id, mount-target status, health checks) feed back to parameterize later steps.
- **Deviation-gating:** replans that deviate from the approved DAG trigger a **fresh approval interrupt** (re-approval card in the UI). Honest partial-failure reporting ("steps 1–2 applied, step 3 failed: …").
- **Hard bounds:** max steps, max replans per step, budget ceiling. Never an unbounded loop.
- **Dependency closure:** missing dependencies resolve user-named value → World Model lookup (ask when several qualify) → stated module default → DAG that creates the dependency first (see [D3](#d3--world-model--reconciliation-engine-resolved-invest--decision-10--l--phase-3) and the Phase-3 roadmap).
**Blast radius:** new loop graph in `agents/`; the single-resource path stays the default. Feature-flagged `AEGISOPS_EXEC_LOOP=off|on`.
**Verify:** "create a VPC and an EC2 inside it" → one DAG approval → both applied in order, EC2 in the new VPC; "…and an EFS mounted on it" exercises replan-on-failure (deviation → re-approval); a mid-DAG failure halts cleanly with what-succeeded reported honestly; bounds enforced and tested.

### U7 — Error recovery + undo · **ANALYSIS §10 §B5/B6** · M · blast: medium
**Change:** (recovery) on a `provider_errors`-classified failure with an obvious fix (bad region/zone, name taken), offer a one-click "retry with <fix>" that re-plans and re-gates — `provider_errors.py` already classifies these. (undo) an "undo last apply" affordance that destroys the just-created resource (or re-applies the previous plan) via the day-2 destroy path — feasible because inventory + per-resource state exist.
**Verify:** a SERVICE_DISABLED/bad-region failure surfaces a retry-with-fix; "undo that" destroys the last resource via the gated destroy flow.

### U8 — Rendering/streaming polish (mostly done; verify) · **ANALYSIS N-04** · S · blast: low
**Now:** markdown rendering (`frontend/components/Markdown.tsx`), streaming cursor, CRLF frame fix, per-message run binding are real and tested.
**Change:** none structural; when the Redis bus (B1) lands, keep the frame contract identical so the store reducer is unchanged; ensure token streaming stays smooth over `XREAD BLOCK`.
**Verify:** existing `tests/sse.test.ts` + `markdown.test.tsx` stay green against the new bus.

---

## D. Data layer

Grounded in [ANALYSIS §07 datastores](../analysis/07_datastores.md).

### D1 — Add hot-path indexes · **§07.1** · S · blast: low
**Now:** missing indexes on the queries run every turn.
**Change (migration):** `messages(session_id, created_at)` (transcript load + `session_messages`), `messages(run_id)` (artifact `_load`), `runs(session_id)` + `runs(org_id, created_at)` (module/overview counts + recent lists). Once M2 lands, index `message_embeddings` (HNSW, like `document_chunks`).
**Verify:** `EXPLAIN` shows index scans on the transcript/run queries; a seeded large session loads without seq-scan.

### D2 — Cross-store atomicity + reconciliation · **P14** · M · blast: medium
**Now:** apply→inventory→graph→trace are separate best-effort writes.
**Change:** write the `resources` inventory row **in the same DB transaction** as the run outcome update (both in `cloudops_execute`/`_persist_result` under one `session_scope`); keep Neo4j/Langfuse as best-effort mirrors. Add a **reconciler** (extend B3's sweeper) that compares `terraform state list` per workspace against the inventory and flags orphans (real resource, no active inventory row) for cleanup.
**Verify:** fault-inject a crash between apply and inventory → the reconciler finds the orphan (or the same-txn write prevents it); `tests/` for the orphan detector.

### D3 — World Model + Reconciliation Engine (resolved: INVEST — decision 10) · L · Phase 3
**Was:** an open invest-or-fold question on a best-effort mirror. **Resolved by the owner: INVEST.**
**Change:** Neo4j stops being a best-effort mirror and becomes the load-bearing **live World Model + Reconciliation Engine**:
- **Contents:** live cloud inventory (all clouds), Terraform state refs, resource **dependency edges** (VPC⊃subnet⊃EC2, SG attachments, DNS→ALB→targets), run/session provenance, incident↔deploy links (later). Ingestion from apply outputs + read-only discovery.
- **Reconciliation Engine:** continuous compare of recorded vs actual — extends `inventory.reconcile` beyond AWS EC2 to **all clouds/types**; drift surfaced as first-class events (UI notification bell + drift panel); **orphan detection** (real resource, no inventory row — closes P14's spend leak, extends B3's sweeper).
- **Consumers:** the Governed Executive Loop plans against it ("which VPCs exist in us-east-1?" answered from the model — the dependency-closure resolution order in the Phase-3 roadmap); **`impact_of(resource)`** gates destroys ("2 resources depend on this — proceed?" on the approval card); memory verification (Context Engine layer 5) grounds recall in reality.
**Honest exit gate:** if graph queries stay 1–2 hops after a quarter of real use, fold to Postgres and drop Neo4j. The investment is conditional on the world model actually being used.
**Verify:** a deliberate manual drift (change an SG in the console) surfaces as a drift notification; destroy of a depended-on resource warns from the world model; orphan sweep finds a resource with no inventory row.

### D4 — Repo/state hygiene · **ANALYSIS §01.5, §13** · S · blast: low
**Now:** dozens of `*.tfplan` files are tracked in git; TF state on a OneDrive bind-mount.
**Change:** `.gitignore` the `*.tfplan` + `terraform.tfstate*` (confirm the latter is ignored), purge tracked plan files from the index, and move dev TF state off the OneDrive path (a Docker named volume) to remove the documented I/O amplification. Pairs with A3 (remote backend for non-dev).
**Verify:** `git status` shows no tracked plan/state; a warm plan is materially faster off OneDrive.

---

## Summary — UX/data effort

| Item | Size | Blast | Phase |
|------|------|-------|-------|
| U1 real policy checks (tier1) | M | medium | 2 |
| U2 SRE real/honest | M | medium | 2 |
| U3 LLMProvider + honest menu | M | medium | 2 |
| U4 ask-which-cloud reachable | S | low | 1 |
| U5 wire/remove interactive input | S | low | 2 |
| U6 Governed Executive Loop | L | high | 3 |
| U7 error recovery + undo | M | medium | 3 |
| U8 streaming polish (verify) | S | low | 2 |
| D1 hot-path indexes | S | low | 1 |
| D2 cross-store atomicity + reconcile | M | medium | 2 |
| D3 World Model + Reconciliation Engine (INVEST) | L | high | 3 |
| D4 repo/state hygiene | S | low | 1 |
