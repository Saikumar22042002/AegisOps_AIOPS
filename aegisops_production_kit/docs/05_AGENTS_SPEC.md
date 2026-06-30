# 05 — Agents Spec (LangGraph multi-agent · real)

Real LangGraph. Gemini `gemini-3.5-flash` is the LLM inside each agent. Native tool-calling.
Durable checkpointing (Postgres + Redis). Human-in-the-loop via LangGraph **interrupts**.
Shared context via Postgres / Redis / Neo4j. Build all of this for real — no simulated graph.

---

## 1. Shared state (`agents/state.py`)
```python
class AgentState(TypedDict, total=False):
    session_id: str; context_id: str; run_id: str
    messages: list           # full thread (LangChain messages)
    intent: str; intent_confidence: float; routing_reason: str
    domain: Literal["cloudops","devops","sre","knowledge","general"]
    workflow: str; workflow_version: str
    raw_inputs: str; parsed_inputs: dict; validation_errors: list
    plan_json: dict; diff: dict; policy_checks: list
    dependencies: list; tool_results: list
    execution_mode: Literal["dry_run","plan","apply","destroy"]
    approval_status: Literal["pending","approved","rejected","not_required"]
    approver: dict           # {user, role, ts}
    errors: list; retries: int
    snow_id: str; trace_id: str; references: list
    confidentiality: dict    # {level, score}
    outcome: dict; resolution: str
```

## 2. Graph (`agents/graph.py`)
```
START → router
router → cloudops | devops | sre | knowledge | general   (by intent + guardrails)
<each action agent> → (needs change?) → approval[INTERRUPT] → execute → verify → finalize
                    → (read-only?) → finalize
finalize → servicenow_update → notify → END
```
- **Checkpoint after every node** (durable). **Interrupt** before any side-effecting tool.
- On `approval=approved` resume from checkpoint and run execute→verify; on `rejected` go to
  finalize with cancelled outcome. **Resumable after restart** (load checkpoint by run_id).
- Every node: open a Langfuse span + OTel span; write a context-graph node; emit SSE events;
  update Prometheus metrics.

## 3. Router agent (`agents/router.py`)
Classify intent into cloudops/devops/sre/knowledge/general with **confidence + reason**
(explainable; shown as interpreted intent, logged). On actionable intent, create a real
ServiceNow SR/CR or Incident with full context. **Misroute target <1%** — log every routing
decision for measurement. Low confidence / ambiguous → ask user to clarify; **no destructive
action on unclear intent.**

## 4. CloudOps agent (`agents/cloudops.py`)
Tools: cloud readers (discovery, availability, drift, verify), `TerraformRunner`,
`AnsibleRunner`, kubernetes (reads + approved applies). Flow:
1. Select workflow template + tool by intent.
2. Request required inputs in a structured format; parse free-form (comma-sep/multiline);
   validate with the workflow's **Pydantic** schema; on error return actionable clarification.
3. Pre-validations + **real availability checks** (SDK reads).
4. Build/select Terraform workspace; `terraform init/validate/plan -json`; parse to diff +
   resource counts + policy checks (OPA/Rego or policy module). Compute confidentiality.
5. Display plan JSON + input JSON for human validation. **Interrupt for approval.**
6. Execution mode: `dry_run` (validate only) | `plan` (stop after plan) | `apply` | `destroy`.
   Apply/Destroy only after approval. Stream CLI output to console SSE.
7. Update monitoring/observability; verify via SDK reads; update + close ServiceNow; capture
   step-level results; write full context graph; return final status.
   (Implements the AC "create vm" example for real.)

## 5. DevOps agent (`agents/devops.py`)
State machine: `INIT → ENSURE_REPO_EXISTS → ENSURE_WORKING_COPY → ENSURE_CHANGES_PUSHED →
ENSURE_CI_RUN → ENSURE_IMAGE_EXISTS → ENSURE_K8S_DEPLOYED`. Real GitHub API + kubernetes.
Collect required secrets up front → store in GitHub Secrets. Create repo if absent; else ensure
Dockerfile + Actions workflows + secrets. Clone; commit; push; trigger + track CI; build +
verify image; deploy to K8s. **Approvals before PR merge, before CI execution, before K8s
deploy.** Track env (dev/stg/prod) + feature branch; share repo link in chat.

## 6. SRE agent (`agents/sre.py`)
Triage true/false positive with rationale; collect real logs/metrics (cloud reads +
Prometheus); RAG-retrieve runbooks; apply decision matrix → next actions; produce
human-readable analysis; propose remediation; execute only after approval; remediation visible
in console stream; correlate with deploys; update + close ServiceNow.

## 7. Knowledge / RAG agent (`agents/knowledge.py`)
Real semantic search over pgvector; return grounded answer + citations to the Analysis/
References UI. No side effects.

## 8. General assistant (`agents/general.py`)
Handles non-actionable Q&A with Gemini, infra-aware persona; no tools with side effects.

## 9. Approval / interrupt (`agents/approval.py`)
LangGraph interrupt presenting plan/diff/policy + execution mode. Resolution via
`POST /approvals/{runId}` (RBAC: only Cloud Architect/Org Admin/Platform Admin). Records
who/when/what immutably; resumable. Password/input prompts use the same interrupt mechanism;
values arrive via `POST /runs/{runId}/input`, masked + never logged.

## 10. ServiceNow + Notification sub-agents
`servicenow_agent.py`: create/update/close SR/CR/INC with artifact links; honor SNOW-side
approvals if configured. `notify.py`: email/chat stakeholder updates.

## 11. Tools — real implementations (`tools/`)
`terraform.py` (init/validate/plan/apply/destroy + JSON parse + state + stream),
`ansible.py`, `kubernetes.py`, `aws.py/azure.py/gcp.py/vmware.py` (read-only discovery/verify),
`github.py`, `prometheus.py`, `console.py` (sandboxed exec + PTY bridge + secret masking).
Each tool: typed inputs (Pydantic), idempotency key, retries/backoff, timeout, structured logs,
Langfuse span. **No tool returns fabricated data.**

## 12. Observability per run (Langfuse + OTel)
One trace per request linked to the context-graph id. Spans for intent/routing/planning/each
step/tool/reasoning/RAG/approval/outcome with token usage + latency, error tags, sanitized I/O.
The artifact "Traces" tab renders this real trace.

## 13. Acceptance behaviors (must hold)
- No infra change without approval; read-only ops run freely.
- Resumable from last successful step after crash/restart; no duplicate execution (idempotency).
- Retry logic + partial-failure handling + rollback where applicable.
- Confidentiality on every agent message; reasoning summary (not raw CoT) in Analysis tab.
- Full context graph + audit per run; SNOW created/updated/closed; observability complete.
