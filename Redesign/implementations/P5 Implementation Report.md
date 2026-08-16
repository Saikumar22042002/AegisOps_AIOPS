# P5 Implementation Report — Production Hardening, Parity & Controlled Cutover

> Branch: `feature/cloudops-v3` · base: `a35a9d5` (P0–P4 uncommitted beneath) · Date: 2026-08-11
> Scope: Redesign/07 Phase 5, per the operator's P5 prompt. The FINAL implementation phase.
> Status: **implemented and verified, uncommitted** — awaiting the operator's single final
> commit after full local validation of P0–P5 together.
> Control ledger: Redesign/11 §33.

## 1. Executive Summary

P5 completes the frozen architecture without introducing a second one: a production
**credential broker** (ADR-17, closes F-20), a **production-config preflight** (hardening), a
deterministic **DEF-19 parity harness** with an honest **cutover decision**, and the P5
frontend/observability slice. The headline P5 judgment: the harness-first **cutover stays
dark** — deterministic parity passes, but live-model parity cannot be proven on this host (the
sandbox Gemini key is dead), and 07 P4.3 mandates dark-until-parity, so `decide_cutover`
returns `may_cutover=False` and `cloudops.py` is not dissolved. That is compliance with the
frozen gate, not a shortfall. P1–P4 remain the only model/reasoning/durability/capability
layers; governance, Terraform safety, and single-user HITL are untouched.

## 2. P5 Requirements Completed

Credential broker + boundary (P5.3/ADR-17); production preflight + `/readyz` surface
(hardening); DEF-19 parity harness + cutover decision; DEF-20 honest multi-cloud matrix;
frontend posture surface; governance-stamp posture flag. No DB change.

## 3. DEF-19 Parity and Cutover

`app/evals/parity.py` compares legacy vs new (harness→engine→packs) topology on dimensions
checkable **deterministically**, classifying each `PASS | FAIL | EXPECTED_DIFFERENCE |
DEFERRED`:

| Dimension | Verdict | Basis |
|---|---|---|
| capability_coverage | PASS | pack read tools cover the legacy read families (Az/GCP storage/db gap = F-12, DEF-20) |
| read_only_boundary | PASS | no mutation tool in the pack read registry |
| mutation_governed | PASS | mutations declared as templates, executed by exec_loop/approval |
| objective_interpretation_deterministic | PASS | provider-neutral objective model maps each case to its family |
| live_tool_selection_parity | **DEFERRED** | needs a live model over the eval dataset |
| live_plan_quality_parity | **DEFERRED** | " |
| live_reasoning_trace_parity | **DEFERRED** | " |
| behavioral_eval_gate_both_topologies | **DEFERRED** | " |

`decide_cutover` → **may_cutover = False** (any FAIL or any DEFERRED live dimension keeps it
dark). **Decision: STAY DARK.** `cloudops.py` retained; `aegisops_capability_packs` stays
default-off. The report makes **no live-parity claim**. When a working model key is supplied,
the harness runs the eval dataset on both topologies; green → flip → dissolve `cloudops.py`
(the single remaining cutover step, now backed by the harness).

## 4. DEF-20 Multi-Cloud Parity

Honest matrix (declared, never faked):

| Family | AWS read | Azure read | GCP read | Mutation (all clouds) | Status |
|---|---|---|---|---|---|
| compute | ✅ | ✅ | ✅ | declared (templates) | READ parity |
| network | ✅ | ✅ | ✅ | declared | READ parity |
| storage | ✅ | ❌ | ❌ | declared | AWS read; Az/GCP DEFERRED (F-12) |
| db | ✅ | ❌ | ❌ | declared | AWS read; Az/GCP DEFERRED |
| k8s | ✅ (+sreops.k8s) | ❌ | ❌ | declared (+day2) | mixed |
| telemetry | sreops.k8s PromQL | — | — | — | k8s |
| repo/ci | devops.github | — | — | declared (PR-first) | github |

Azure/GCP storage/db/k8s **read** tools are genuinely absent in the underlying readers (the
pre-P4 F-12 asymmetry) — declared in the packs, never invented. Three-cloud read/verify parity
is the P5.1 gate (DEF-20). The generic layers stay provider-neutral (grep-verified).

## 5. Credential Broker

`app/security/credential_broker.py` (ADR-17, closes F-20): `CredentialGrant` is
**redaction-safe by construction** — `repr`/`str` never reveal material (verified: no leak
into logs, traces, SSE events, EvidenceCards, prompts, or errors); the ONE authorized egress
is `provider_env()` to the Terraform subprocess. `resolve(org, provider, env, operation)`
returns a scoped grant and audits it with a **non-secret** fingerprint. `EnvBackedBroker` is
the dual-path default (issues the process's configured set — **byte-identical** to the
pre-broker path, proven by `test_terraform_env_is_byte_identical_across_broker_flag_dual_path`);
a vault/STS backend (AssumeRole / Azure SP+workload-identity / GCP SA impersonation) plugs in
behind `CredentialBroker` (the ADR-17 sign-off item; real federation needs a vault + cloud —
DEF-21). `terraform.py._env` uses the one source-of-truth env mapping in both modes; a per-org
grant overrides via `set_credential_grant`. Gated by `aegisops_credential_broker` (default off).

## 6. Production Hardening

`app/preflight.py`: `run(settings)` validates event-bus (no non-local memory), metrics-auth
(F-16), permission-mode (**AUTONOMOUS never permitted**), no-four-eyes (setting must not
exist), strict tenancy, worker-role ownership (F-18), and credential-broker posture. Local is
lenient (warnings); non-local **blocks** on unsafe posture. Logged at startup (the P0 event-bus/
Redis refusals remain the hard gate) and surfaced on `/readyz` alongside dependency
reachability.

## 7. CloudOps Final State

Read via the AWS/Azure/GCP packs (behind the dark flag) or the retained legacy `cloudops.py`
(default). Mutation stays the governed exec_loop → approval → Terraform path; the credential
broker now scopes the Terraform subprocess credentials (dual-path). Cutover to packs-as-spine
deferred (DEF-19).

## 8. DevOps Final State

`devops.github` pack: read (repo/CI status); change capabilities declared PR-first, governed.
GitHub Actions log-download/rerun breadth (07 P5.2) not expanded — recorded deferred.

## 9. SREOps Final State

`sreops.k8s` pack: read/investigation (namespaces/pods/PromQL) via the harness INV loop;
day-2 remediation declared, governed. Alertmanager→incident pipeline (07 P5.4) not built —
deferred (needs live alerting + the mutation-in-packs cutover).

## 10. P1/P2/P3/P4 Integration

Unchanged and authoritative: P1 the only model layer, P2 the only reasoning loop, P3 the only
durable engine, P4 packs the only domain capability boundary. P5 added hardening + the broker +
the parity harness around them — verified no rewrite, no second framework/loop/abstraction,
LangGraph untouched (6 importers, spine zero-diff).

## 11. Frontend/API Integration

`GET /capabilities` extended with a `posture` block (permission_mode, credential_broker,
durable_engine, approval_model); the ModuleView `CapabilitiesPanel` renders posture chips
alongside the multi-cloud parity matrix. Governance stamp gains `credential_broker`. tsc
clean; additive — no consumer broken.

## 12. DB/Redis Changes

**None.** P5 is code + config only. Redis usage unchanged (broker adds none). No migration.

## 13. Governance/Safety

Single-user HITL intact (preflight asserts the four-eyes setting does not exist; stamp
`approval_model: "hitl"`); AUTONOMOUS never enabled (preflight blocks the mode; policy never
grants autonomous mutation); destructive always human-gated; ESTOP present. Terraform mutation
boundary, plan_guard, tenancy/RBAC, idempotency, redaction, investigation read-only boundary —
all untouched (safety_invariants + tenancy + redaction suites green in the container tier).

## 14. Security

The credential broker is the F-20 remediation: credentials never enter prompts/logs/traces/
events/frontend/evidence (redaction-safe grant, verified); secret scanning unchanged; sandbox
credentials not rotated/revoked/printed per the operator classification; no credential
committed. No credential was discovered that could not be classified sandbox-only, so no
security blocker is raised.

## 15. Observability and Cost

run_id/task_id/step_id correlation, Langfuse traces, and the authoritative `llm_usage` ledger
are unchanged; the broker audits grants (non-secret) to `audit_log`; posture flags on
`/healthz` and every approval card; preflight findings on `/readyz`. Cost accounting stays the
ledger; Langfuse stays observability.

## 16. GitNexus Impact Analysis

New surface (`credential_broker`, `preflight`, `evals/parity`) + one flag-gated dual-path edit
to `terraform.py._env`. Generic layers provider-neutral (grep-clean); LangGraph spine and the
rest of the mutation path zero-diff.

## 17. Dead-Code / Transitional Cleanup

None removed. All transitionals (T-01, T-P2-01, T-P3-01, T-P4-01) remain until their
parity/removal conditions — `cloudops.py` explicitly retained (its removal is the DEF-19
parity-gated cutover).

## 18. Behavioral Parity Evidence

Terraform credential env byte-identical across the broker flag (dual-path, pinned); broker
grant redaction-safe + per-org override (pinned); preflight severities local/non-local
(pinned); parity harness deterministic dims PASS + live dims DEFERRED (pinned); cutover
decision stays dark (pinned). Container tier: safety_invariants/tenancy/redaction/security all
green — the mutation-boundary edit changed no behavior.

## 19. Test and Regression Results

`test_p5_hardening.py`: 12 tests (broker boundary + dual-path + vault-pluggability, preflight
severities incl. AUTONOMOUS-block, parity dims + cutover decision) — all green. Container tier:
**166 passed / 0 failed** (P5 + all phases + safety/redaction/security/health, live
datastores). Full local regression: **1184 tests — 960 passed / 50 failed / 174 skipped**
(21:00); **INTRODUCED_BY_P5: 0** (programmatic diff vs baseline — empty; zero failures in any
phase suite; all 50 confined to the documented terraform-provider environment tiers, identical
grouping to the P2/P3/P4 runs — CI with providers is the authoritative signal for that tier).

## 20. End-to-End Runtime Evidence

The read-flow end-to-end (objective → pack → harness → verify → evidence) is CONTAINER-PROVEN
and FIXTURE-PROVEN (scripted model over the real control flow). Flows A–E's *live* execution
(real cloud mutation, live-model reasoning) is **DEFERRED** — the dead sandbox key and the
parity-gated mutation cutover block live proof on this host; the durability mechanism (crash→
recover→no-double-apply) and governance path are proven at the mechanism level. No live proof
is claimed where none exists.

## 21. Rollback Strategy

Entire P5 surface additive + uncommitted → discard reverts it. Committed: three default-off
flags (`aegisops_credential_broker`, plus the P2–P4 flags) keep every new path dark; no
migration to roll back; the broker's dual-path default is byte-identical to the pre-broker
path; the preflight only warns/blocks, changing no execution behavior.

## 22. Remaining Limitations

Honest proof-level ledger: **STATICALLY-VERIFIED / FIXTURE-PROVEN / CONTAINER-PROVEN** —
broker boundary, dual-path env, preflight, parity dims, all P0–P5 suites (166 container).
**DEFERRED (needs a working model key):** the DEF-19 live eval-parity and therefore the
production-spine cutover + cloudops.py dissolution. **DEFERRED (needs live cloud/vault):** the
real vault/STS credential backend (DEF-21) and Azure/GCP read breadth (DEF-20, P5.1). **NOT
LIVE-PROVEN:** any live cloud mutation or live-model flow on this host (dead key + parity gate).
None is a P5 code defect; each is an environment/parity gate recorded in doc 11 §21.

## 23. P0→P5 Final Architecture Boundary Audit

The complete system: P1 provider substrate → P2 harness (only reasoning loop) → P3 durable
engine (only workflow engine) → P4 capability packs (domain boundary) → P5 hardening + broker,
all governed by single-user HITL, policy/permission modes, Terraform-only mutation, tenancy/
RBAC/redaction/audit, with LangGraph retained as the isolated interrupt/checkpoint substrate
(6 importers, spine zero-diff across P1–P5). No second framework, loop, planner, LLM
abstraction, or workflow engine was introduced across any phase. Four-eyes absent throughout.
AUTONOMOUS never enabled.

## 24. Final Verdict

**P5 COMPLETE — READY FOR ACCEPTANCE**

The credential broker, production preflight, DEF-19 parity harness, and frontend posture slice
are implemented, verified, and green (12 P5 tests + 166 container tier). The cutover decision
is the correct, compliant **stay-dark** per 07 P4.3 — deterministic parity proven, live parity
honestly deferred on a dead model key, `cloudops.py` retained. Governance, Terraform safety,
single-user HITL, and the P1–P4 layers are untouched; the product runs with every new flag off.
The remaining open items (live cutover, real vault backend, Azure/GCP read breadth) are
environment/parity-gated, documented, and executor/backend-injection ready. Work is
deliberately uncommitted, awaiting the operator's single final commit after full local
validation of P0–P5 together. STOP after this report — no self-audit, no post-P5 cleanup, no
commit, no push.
