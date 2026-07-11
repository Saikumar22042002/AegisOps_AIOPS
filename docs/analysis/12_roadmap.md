# 12 — Roadmap to production-grade

[← back to index](../../ANALYSIS.md)

Phased, prioritized, tied to the findings in [09](09_problems.md) and [10](10_gap_analysis.md). Three tranches: **must-fix to be trustworthy** → **needed to be competitive** → **nice-to-have**.

## Tranche 1 — Must-fix to be trustworthy (weeks, blocking)

These are correctness/security defects that make the current claims false. Ship nothing to a second customer until these are closed.

1. **Real multi-tenancy** (P2). Derive org from the authenticated user (`keycloak_sub`→`users.org_id`), scope every query, populate `Session.user_id`. This is the foundation the next two depend on.
2. **Authorize reads + credential reveal** (P1, P3). Owner/org predicate on session/run/stream getters; approver/owner gate + audit on `/runs/{id}/credentials`. Return 404 on mismatch.
3. **Fix the idempotency double-apply** (P5). Wait-or-abort on in-flight claims; reject a second `/approvals` while one runs.
4. **Stop blocking the event loop** (P6). Thread-offload the inventory reconcile.
5. **Evict SSE channels** (P4, first half). Call `drop_channel` on close with a reconnect grace window — stops the memory leak even before the Redis-bus work.
6. **Make governance honest** (P7, P8). Either implement real SRE remediation + real policy evaluation, or label them "proposed/not-evaluated" so approvers aren't misled. A governance product cannot ship fake policy checks.
7. **Remote Terraform state + locking** (P12). Default to S3+DynamoDB (already env-plumbed); unique plan-file path per resource.

*Rationale:* 1–3 are security; 4–5 are reliability under any real concurrency; 6 is the integrity of the core value prop; 7 prevents state corruption. All are contained changes to existing code.

## Tranche 2 — Needed to be competitive (the ChatGPT/Claude-Code bar)

8. **Latency pass** (10 §A1). Skip `terraform init` when initialized; `TF_PLUGIN_CACHE_DIR` on a volume; TF state off OneDrive. Biggest felt win; ~13–19s/turn.
9. **Redis-backed event bus** (P4, second half). Move `_channels`/replay to Redis streams so the API is truly stateless/horizontally scalable and reconnect works across workers.
10. **Better memory** (10 §A2/A3). Token-budgeted transcript + rolling summary of dropped turns; router sees a retrieval over the thread, not just 8 turns; add per-user cross-session memory (build on the existing inventory).
11. **Real LLM provider abstraction** (P10). `LLMProvider` interface, honor `body.model`, implement or trim the model menu. Required to credibly call itself model-agnostic.
12. **Bounded multi-step planner** (10 §B4). Let CloudOps emit an approval-gated DAG (VPC→subnet→EC2) instead of one resource per turn. This is the one structural change; scope it as *bounded + gated*, not autonomous.
13. **Auto-recovery + undo** (10 §B5/B6). Retry-with-fix on classified provider errors (bad region/name); "revert last apply." `provider_errors` + per-resource state already make these feasible.
14. **Wire the honest surfaces** (P9, P13). Real Traces tab from Langfuse; wire or remove mid-run interactive input.
15. **Cross-store atomicity + reconciliation** (P14). Inventory row in the same txn as the run outcome; a periodic TF-state-vs-inventory reconciler to catch orphans/spend.

## Tranche 3 — Nice-to-have / hardening

16. **4-eyes** (P15): record initiator; optional approver≠initiator for prod.
17. **Modify beyond ports**: S3 lifecycle, RDS scaling, tags — the modify framework exists (per-resource state + guard); extend `_modify_resource` per type.
18. **Live reconcile beyond AWS EC2** (`inventory.reconcile`, `finalize._reconcile_checks`): add Azure/GCP branches so reads/verify are live cross-cloud.
19. **Cost estimation**: the UI implies "$/mo within guardrail"; implement real estimation (Infracost or provider pricing) feeding the policy check + approval card.
20. **Neo4j decision** (02): either build the cross-run/incident-graph features that justify it, or fold provenance into Postgres and drop the dependency.
21. **Observability polish** (P19): exempt/tune rate-limit on the SSE route; wire or remove unused metrics; plan Langfuse v2→v3.
22. **Notify recipients** (P17); **persist-time redaction backstop** (P20); **DevOps CI polling** (P16); **Ansible** wired or removed.
23. **Repo hygiene**: remove committed `*.tfplan` files from git; confirm `terraform.tfstate*` stays ignored; the OneDrive working path is a dev-only hazard.
24. **Kill-mid-interrupt restart test** and **full apply→day-2 browser E2E** (the two honestly-noted test gaps) once Tranche 1 lands.

## Sequencing rationale
Tranche 1 is non-negotiable and mostly small. Tranche 2 items 8–11 are independent and parallelizable; item 12 (multi-step planner) is the largest and should follow the memory/latency work so it inherits a fast, continuous base. Tranche 3 is opportunistic. The single most valuable *architectural* decision is #12's scope: commit to **bounded, approval-gated multi-step** — it's what separates "provision one resource with a gate" from a real infrastructure agent, without abandoning the safety posture that is this codebase's best feature.
