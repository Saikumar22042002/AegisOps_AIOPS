# 05 — Tool and Agent Contracts

> Normative schemas for the surfaces the harness (04) executes and the packs (02 §4) populate.
> Everything here is typed, versioned, and CI-checkable. JSON Schema is the single tool-shape
> dialect (adapters translate per wire family).

---

## 1. ToolDef (the tool registry record)

```python
ToolDef:
  name: str                          # namespaced: "aws.ec2.describe_instances", "github.actions.download_log"
  description: str                   # model-facing; imperative, includes when-NOT-to-use
  input_schema: JSONSchema           # validated before execution; unknown fields rejected
  output_schema: JSONSchema          # validated after execution; result normalized to it
  effect: read | propose             # LLM-facing registry holds ONLY these two (see §2)
  risk: read | low | medium | high | destructive
  side_effects: [str]                # declared, human-readable ("creates ServiceNow ticket")
  providers: [str]                   # which cloud/service backends serve it ("aws", "github", …)
  credentials: CredentialRef         # broker handle — NEVER raw secrets; org-scoped at execution
  timeout_s: int = 30
  retry: RetryPolicy                 # default: no retry for propose; 2× jittered for idempotent reads
  idempotent: bool = True            # reads true; propose true (same draft twice is safe)
  verify: VerifyStrategy | None      # §8 — post-conditions attached by middleware
  rate_key: str | None               # per-org rate bucket
  redact: bool = True                # output passes redaction before transcript/trace
  cost_hint: cheap | normal | expensive   # scheduler + disclosure ordering hint
  requires_capabilities: [str]       # e.g. ["kubeconfig"] — availability gate at schema build (check_fn)
```

Registration-time enforcement (inherits `investigation.py`'s proven bones): mutation-shaped names
rejected in the LLM-facing registry by the 20-marker denylist; the registry **freezes** at run
start — a running agent cannot grow its tool surface; schema build honors `requires_capabilities`
so unavailable tools never appear (no "tool exists but always errors" noise).

## 2. Effect classes vs. tool policies (two taxonomies, resolved)

The prior blueprint conflated these (its inconsistency I2). They are orthogonal:

- **Effect** (property *of a tool*): `read` — observes the world; `propose` — produces data for
  human approval (`propose_goal_dag`, `estimate_cost`, `policy_precheck`, `propose_day2_action`).
  `governed_mutation` is **not an effect in this registry**: mutations exist only as engine
  executor entries (Terraform templates, K8s catalog operations, day-2 verbs) keyed by approved
  plans. No LLM-facing registry ever contains one.
- **ToolPolicy** (property *of an agent spec*): `NONE` (router/critic/judge — no tools),
  `READ_ONLY_FROZEN` (investigator, SRE triage — read-effect only, frozen registry, shared call
  budget), `GOVERNED_PROPOSE` (main loop, planner — read + propose effects).

## 3. Execution middleware (order is normative)

```
tenancy_scope → rbac_check → policy_verdict → rate_limit → timeout → execute
             → output_validate → redact → audit → observe(emit)
```

- Failure at any stage returns an **observation** (`ToolObservation{ok=False, stage, error}`),
  never an exception (L3). The audit stage writes the `audit_log` row for every call — closing
  the two-call-site audit gap found in the current system.
- Cancellation: cooperative; checked before execute and honored between tool calls. A tool
  exceeding `timeout_s` is cancelled and reported as a timeout observation.
- Rate limits are Redis-backed per `rate_key` × org (replaces the per-process slowapi store, F-17).

```mermaid
flowchart LR
    subgraph REG["Tool Registry (frozen per run)"]
        SCH["schemas_for(spec, turn_filter)<br/>policy-filtered · availability-gated ·<br/>progressive disclosure"]
    end
    LLM["model (native FC)"] -->|tool_use| PV["policy verdict<br/>(layered pipeline, 04 §8.3)"]
    SCH --> LLM
    PV -->|allow| MW["middleware chain<br/>tenancy→rbac→rate→timeout→execute→<br/>validate→redact→audit→observe"]
    PV -->|approval| INT["durable interrupt"]
    PV -->|deny| OBS2["denial observation"]
    MW --> OBS["ToolObservation (ok | error)<br/>→ run log → next context"]
    OBS --> LLM
    OBS2 --> LLM
```

## 4. Discovery, filtering, disclosure

1. **Static:** `schemas_for(spec)` = registry ∩ spec.tool_policy ∩ pack activation ∩ availability.
2. **Turn-scoped narrowing:** the `before_prompt_build` seam may narrow further (e.g., during
   parameter collection only `extract`-relevant tools are mounted).
3. **Progressive disclosure:** when mounted schemas would exceed ~10% of the bound model's context
   window, non-core tools are replaced by three bridges — `tool_search(query)`,
   `tool_describe(name)`, `tool_call(name, args)` — with core loop tools and the active pack's
   primary tools never deferred (Hermes). Bridge calls pass the identical policy/middleware path.
4. **MCP:** external MCP servers mount as `read`-effect tools only, behind an org-admin allowlist;
   mutation-shaped names refused at registration; MCP results are untrusted input (redaction +
   injection-stripping on ingest).

## 5. Trust-boundary payloads (propose-effect outputs)

```python
GoalDAGDraft:                        # the ONLY way infrastructure change is initiated
  objective_id: str
  steps: [StepDraft{ id, kind: module|day2|k8s, template_key,   # must exist in approved catalog
                     params, wires, depends_on, rationale }]
  on_failure: halt | rollback | continue_independent
  window: ChangeWindow | None
# → engine.compile_goal_dag(draft) → Workflow | RefusalReport (catalog/wiring/guard/
#   compensation/lock closures — 07 P3.1). Refusals are observations; the loop may revise (L4).

Day2Proposal:
  verb_key: str                      # must exist in DAY2_ACTIONS registry
  target: ResourceRef
  rationale: str
  evidence_refs: [EvidenceRef]       # remediation must cite evidence
```

The compiled plan hash is what approval binds to (04 §8.4). Post-approval, the engine executes
deterministically; divergence ⇒ deviation ⇒ re-approval.

## 6. Agent contracts

```python
AgentSpec        # 04 §2 — declaration only
AgentResult:                          # subagent → parent; size caps enforced
  status: answered | budget | failed | needs_input
  findings: str                       # ≤ 32k chars
  evidence_refs: [EvidenceRef]        # pointers into the child's run log — never raw transcripts
  confidence: low | medium | high
  usage: Usage                        # rolls up into parent ledger, agent_kind='subagent'
SpawnRequest:
  spec: AgentSpecRef; subgoal: str (≤16k); context_slice: CuratedSlice (≤32k)
  # blocked in child: delegate, ask_user, memory-write, channel-send, schedule
  # child output is UNTRUSTED EVIDENCE — cannot override policy/system instructions
```

## 7. Policy contract

```python
PolicyRequest:  {principal, org, env, mode, action: ToolCall|StepRef, risk, blast_radius?}
PolicyVerdict:
  decision: allow | approval_required | deny | hardline_deny
  reasons: [str]                      # every verdict explainable; logged + auditable
  approval_tier: SINGLE_DAG | PER_STEP_HIGH | PRE_APPROVED | None
  bound_hash: str | None              # what an approval, if granted, binds to
```

Deterministic; pure function of (request, org policy pack, platform baseline). LLM risk
assessments may appear in `reasons` and may raise `risk`, never lower a decision's severity.
`hardline_deny` is unappealable by construction (04 §8.2).

## 8. Verification contracts

```python
VerifyStrategy:                       # declared per tool/template/day-2 verb
  checks: [Check]                     # SDK reads, HTTP/TCP probes, PromQL assertions, rollout status
  timeout_s: int; grace_s: int        # one re-verify after grace before failure
EvidenceCard:                         # what verify PRODUCES — never a bool
  subject: ResourceRef | ServiceRef
  checks: [{check, expected, observed, ok, at, source_tool}]
  verdict: verified | failed | partially_verified | unverifiable   # honest states
  collected_by: str                   # tool/executor identity — independent of the actor that mutated
Check evaluation rule: evidence is collected by READ tools independent of the mutation path;
"unverifiable" is an honest first-class outcome (no fake passes — Neo4j-down lesson).
```

## 9. Prompt registry

`PromptRef(name, version)` → registry table row: content, content_hash, owner, changelog,
eval_state. Recorded on every ledger row (`prompt_version`) and Langfuse generation. Prompts
change only via PR + eval gate — they are versioned artifacts, not string literals (closes
"which prompt caused this regression?").

## 10. Capability-pack registration contract

A pack module exports exactly: `TOOLS: [ToolDef]`, `KNOWLEDGE: [PromptFragment]`,
`PLAYBOOKS: [ProcedureRef]`, `VERIFY: {resource_type: VerifyStrategy}`, `TEMPLATES: [TemplateKey]`,
`DAY2: [Day2VerbKey]`, `POLICIES: [PolicyFragment]`. CI enforces: no loop code, no SDK dispatch
outside ToolDef `fn`s, no imports from `harness` internals beyond the public contract, parity
manifests per service family (03 §3.4). A pack is reviewable data; the kernel is the only engine.
