# P4 Implementation Report — Domain Capability Migration (harness-first inversion)

> Branch: `feature/cloudops-v3` · base: `a35a9d5` (P0–P3 uncommitted beneath) · Date: 2026-08-11
> Scope: Redesign/07 Phase 4, per the operator's P4 prompt.
> Status: **implemented and verified, uncommitted, awaiting operator acceptance.**
> Control ledger: Redesign/11 §32.

## 1. Executive Summary

P4 begins the mandate's core move (00 §3): CloudOps/DevOps/SREOps become **thin capability
packs** that contribute tools + knowledge, while the P2 harness owns reasoning, the P3 engine
owns durability, and P1 owns models. A new `app/packs/` package holds AWS/Azure/GCP/K8s/GitHub
packs as equal first-class specialists; the P2 harness's INV **read registry is now sourced
from the packs cloud-neutrally**; a provider-neutral **objective model** maps intent
("create a VM", "find my VPC") to a capability family and provider; and a **permission-mode
matrix + ESTOP** governs execution with AUTONOMOUS never enabled. This ships **dark behind a
flag** (`aegisops_capability_packs`, default off) exactly as 07 P4.3 mandates — the
production-spine cutover and `cloudops.py` dissolution happen only at proven eval parity,
which is correctly deferred (the dead sandbox key blocks live-model parity on this host).

## 2. P4 Capabilities Migrated

Capability-pack contract (02 §4); five packs (cloudops.aws/azure/gcp, sreops.k8s,
devops.github); the harness read surface sourced from pack read tools; the provider-neutral
objective/capability-family model; the four-mode permission matrix + ESTOP; a capabilities
API + a frontend multi-cloud parity panel; governance-stamp posture flags. Read paths are
migrated; mutation is **declared** (approved Terraform templates + day-2 verbs) and stays the
governed exec_loop/approval/P3 path.

## 3. Old → New Architecture Mapping

| Old | New |
|---|---|
| `agents/cloudops.py` (1,531 LOC) read/regex/plan | thin `packs/cloudops/{aws,azure,gcp}` read tools + declared mutation; harness reasons (cloudops.py RETAINED behind the flag until parity) |
| hardcoded `investigation.default_registry` | `packs.registry.build_read_registry` — pack-sourced, cloud-neutral |
| provider named in agent code ("EC2") | provider-neutral `objective` (family "compute") → pack resolves per cloud |
| ad-hoc mode handling | `harness/policy.py` four-mode matrix + ESTOP |
| SRE hardcoded telemetry | sreops.k8s pack read tools (namespaces/pods/PromQL) |

## 4. CloudOps Migration

AWS, Azure, GCP packs register their read tools (networks, compute, storage, db, k8s
clusters) wrapping the existing `tools/{aws,azure,gcp}.py` readers — provider-specific logic
stays inside the pack. Mutation capabilities are declared as approved Terraform template keys
(`aws.vpc`, `azure.vm`, `gcp.gke`, …) but carry no `fn` and never enter the read registry —
the Terraform mutation boundary is untouched; real apply remains the governed path. The
existing `cloudops.py` remains the default read path (flag off) until eval parity.

## 5. DevOps Migration

`devops.github` pack: read tools (repo existence, Actions run status); change capabilities
(`open_pr`, `dispatch_workflow`) declared as propose specs — PR-first governed flow, direct
default-branch pushes remain policy-banned. Not executed as read tools.

## 6. SREOps Migration

`sreops.k8s` pack: read/investigation tools (namespaces, deployments, pods, PromQL
telemetry) feed the harness INV loop — investigation produces structured observations +
evidence, not model prose. Day-2 remediation verbs (restart/scale/rollback) declared, gated
to the governed path. The read-only investigation boundary is preserved (registry rejects
mutation-marker names at registration).

## 7. P2 Harness Integration

The P2 harness is unchanged and the only reasoning loop. P4 changes only its **tool source**:
when the flag is on, `harness.inv._read_registry` builds the frozen read registry from packs
instead of the hardcoded default. No second loop, no planner, no LLM abstraction, no model
router, no tool-calling framework was added (verified: packs import no harness/engine
internals; generic layers contain zero provider vocabulary).

## 8. P3 Durable Execution Integration

Read investigations run through the harness (which the P3 driver already wires as a step
executor). Mutation workflows continue to route loop → propose_goal_dag → P3 engine →
approval; packs supply the template/day-2 metadata the engine's compile step consumes. No
bypass of Task/Run/Step/run_events/recovery.

## 9. Frontend / API Integration

New additive `GET /capabilities` returns the provider-neutral parity matrix (pack × families
× configured × templates/day2 × `packs_enabled`). The frontend `CapabilitiesPanel`
(infrastructure view) renders it — configured providers green, unconfigured listed honestly
(no fake support). The governance stamp gains posture keys (`capability_packs`,
`permission_mode`, `harness_read_paths`, `durable_engine`) so a cutover can never drift
silently. tsc clean; existing consumers unaffected (additive).

## 10. DB / Redis Changes

**None.** P4 is code + config only — no migration, no schema change, no new Redis usage. The
pack read registry reuses the P2 harness path; mutation reuses the P3/exec_loop path.

## 11. Governance / Safety Verification

Four-eyes remains absent (unchanged); single-user HITL intact; approval/plan_guard/terraform
zero-diff. The permission matrix gates every mutation to approval; **destructive risk is
always human-gated even under AUTONOMOUS+allowlisted**; AUTONOMOUS never grants a mutation in
this build (no allowlist is assembled); ESTOP denies new mutations while allowing reads.
Read-only-by-construction is doubly enforced (effect filter + the investigation denylist).

## 12. Observability

Pack read tools run inside the harness INV loop → typed `run_events` + P1 ledgered
generations + run_id correlation, unchanged. The new posture flags are on `/healthz` and every
approval card. No new path is invisible.

## 13. GitNexus Impact Analysis

New surface (`app/packs`) with one flag-gated integration into `harness/inv`. Generic layers
show zero provider leakage; the six LangGraph spine files and `terraform.py` are zero-diff.

## 14. Dead-Code Decisions

None removed. `cloudops.py` and the default registry are **TRANSITIONAL** (T-P4-01), retained
as the coexisting default until eval parity — deleting them is the deferred cutover (DEF-19),
not P4-now.

## 15. Transitional Components

T-P4-01: legacy cloudops.py/default-registry read path ↔ pack read path, behind
`aegisops_capability_packs` (default off). Owner: packs. Removal condition: eval parity on
both topologies. Rollback: flag off. P0–P3 transitionals (T-01, T-P2-01, T-P3-01) still stand.

## 16. Behavioral Parity Evidence

The end-to-end read flow is proven: "find my Azure VMs" → objective resolves azure/compute →
the harness INV loop reasons over the **pack** read tool → evidence-backed answer citing the
observation. Multi-cloud registration parity is proven (all three cloud packs contribute read
tools equally); credential-gating honesty is proven (GCP unconfigured lists but contributes no
callable tools). Read-only boundary, mutation-declared-not-executed, permission matrix, and
ESTOP are all pinned. Flag off = byte-identical legacy behavior (coexistence).

## 17. Test / Regression Results

`test_p4_packs.py`: 17 tests (pack contract, 3-cloud parity, read-only-by-construction,
mutation-declared, objective model parametrized, permission matrix + ESTOP, end-to-end read
flow) — all green. Container tier (live datastores): **159 passed / 0 failed** across P4 +
P3/P2 + governance/safety/tenancy. Full local regression: **1172 tests — 948 passed / 50
failed / 174 skipped** (20:51); **INTRODUCED_BY_P4: 0** (programmatic diff vs baseline —
empty; zero failures in any phase suite; all 50 confined to the documented terraform-env
tiers, identical grouping to the P2/P3 runs).

## 18. Runtime Smoke Tests

Backend imports clean with packs; `GET /capabilities` returns the 5-pack matrix; the
governance stamp carries the posture flags; tsc clean. (A live pack-driven harness run needs a
working model key — the dead sandbox key blocks it host-wide, pre-P1.)

## 19. Deferred Work

DEF-19 (the parity-gated cutover: cloudops.py dissolution + loop-as-production-spine + real
mutation migration into packs + P4.4 planner/critic + P4.7 eval expansion) — 07 P4.3 mandates
dark-until-eval-parity, and parity cannot be proven without a live model key. DEF-20 (Azure/GCP
storage/db/k8s **read** tools — the F-12 asymmetry, genuinely absent, declared honestly; P5.1
parity gate). Both recorded in doc 11 §21 with target phases.

## 20. P4 → P5 Boundary Verification

No credential broker, no production hardening, no final deployment topology, no enterprise
tenancy hardening, no unrelated modernization. No LangGraph replacement/removal, no second
reasoning loop or workflow engine, no new LLM abstraction, no P2/P3 rewrite, no governance or
Terraform-safety bypass, no broad dead-code deletion.

## 21. Rollback Strategy

Entire P4 surface is additive and uncommitted → discard reverts it. Committed:
`aegisops_capability_packs=off` disables the pack read path (cloudops.py default unchanged);
no migration to roll back; the new endpoint/panel/posture-keys are additive with no other
consumer.

## 22. Known Limitations

The harness-first inversion ships **dark** (as the frozen plan requires): the read-path pack
sourcing, objective model, and permission modes are live behind the flag, but the production
cutover (cloudops.py dissolution, loop-as-spine, real mutation-in-packs) is parity-gated and
deferred — provably so, because the dead sandbox Gemini key makes live eval parity impossible
on this host. Azure/GCP read breadth (storage/db/k8s) is genuinely limited (F-12), declared
honestly rather than faked; three-cloud read parity completes at P5.1. Live pack-driven
harness runs await a working provider key.

## 23. Final Verdict

**P4 COMPLETE — READY FOR ACCEPTANCE**

The capability-pack structure, multi-cloud read migration (AWS/Azure/GCP equal first-class),
provider-neutral objective model, and permission-mode matrix + ESTOP are implemented,
verified, and green (17 pack tests + 159 container tier; 0 regressions introduced). The
generic layers are provider-neutral; the P2 harness is the only reasoning loop; mutation and
governance are untouched; the product runs with the flag off. The parity-gated cutover
(cloudops.py dissolution + loop-as-production-spine) is correctly deferred dark behind the
flag per 07 P4.3, gated on eval parity a working model key provides. Work is deliberately
uncommitted pending operator acceptance. Stopping at the P4 gate — P5 is not started.
