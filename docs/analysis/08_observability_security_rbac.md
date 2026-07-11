# 08 — Observability, security, RBAC

[← back to index](../../ANALYSIS.md)

## 8.1 Observability — does it actually fire?

**Langfuse (real, per-run span tree).** `integrations/langfuse_client.py` builds one trace with `id == run_id`. `runner.begin_run`/`end_run` open/close it; `agents/timing.py:start_step`/`end_step` open/close a **span per graph node/sub-step** with deterministic ids (`<run_id>:<step>`) so the resume closes the approval span across the human interrupt with the true wait. LLM **generations** are recorded at the Gemini chokepoints (`integrations/gemini.py:agenerate` and `agents/llm.py:stream_answer`), including per-retry ERROR generations, with token usage from `usage_metadata` and **USD cost** computed from `GEMINI_COST_PER_1M_*`. **Tool spans** wrap terraform (`tools/terraform.py:_span`), ServiceNow (`integrations/servicenow.py:_span`), RAG retrieve (`rag/retriever.py`), and cloud availability (`cloudops.py:_availability`). All payloads pass redaction. If Langfuse is unconfigured, everything is a no-op.

- **Does it fire?** Yes — this is real and well-tested (`tests/test_langfuse_tracing.py` includes a live round-trip that reads the tree back). The historical "0 traces" was root-caused to **wrong project keys** (the `.env` key belonged to a different project) + shallow instrumentation, both fixed. **Caveat:** it's Langfuse **v2**; and the "trace_id == run_id" identity is the whole design, so a run must have a unique id (it's a UUID, fine).
- **Gap:** the product's **Traces artifact tab does not read Langfuse** — `api/artifacts.py:traces` returns hardcoded span names (`intent.classify`, `agent.route`, …) with `—` durations. So the real, rich traces exist in Langfuse but the in-app tab shows a static placeholder.

**OpenTelemetry.** `otel.py` sets up OTLP/gRPC exporters to the collector; `FastAPIInstrumentor` auto-instruments requests (health/metrics/readyz excluded); `runner.py` opens an `agent.run` span. The collector re-exports metrics in Prometheus format (`infra/otel/otel-collector.yaml`); traces/logs go to collector stdout (`debug` exporter — **not** persisted to Tempo/Loki, which are "wired later"). So OTel **metrics** reach Prometheus, but OTel **traces** are effectively debug-only; the real trace story is Langfuse.

**Prometheus / Grafana.** Custom registry (`metrics.py`): `aegisops_api_requests_total`, `..._request_duration_seconds`, `aegisops_agent_runs_total`, `..._agent_step_duration_seconds`, `aegisops_llm_latency_seconds`, `aegisops_rag_latency_seconds`, `aegisops_approval_wait_seconds`, `aegisops_tool_retries_total`, `aegisops_dependency_up`. `/metrics` serves them; Prometheus scrapes host + compose targets; Grafana dashboard `aegisops-overview.json` is provisioned with real PromQL panels. **Fires? Yes** for API + agent-run + latency counters. Some declared metrics are under-used (`TOOL_RETRIES` is never incremented; `AGENT_STEP_DURATION` isn't observed — timing goes to `run_steps`/Langfuse instead), so a few dashboard series will be empty.

**Structured logs.** `logging_conf.py` — structlog JSON to stdout with a contextvar carrying correlation ids (request_id/run_id/session_id/user). Real and consistent.

## 8.2 Secret handling & redaction

**Redaction** (`security/redaction.py`): `redact()` masks private-key blocks (keeps BEGIN/END), AKIA/ASIA access keys, bearer tokens, secret-shaped `key=value`/`"key":"value"` (2-group value-mask — the leak-the-value bug is fixed), JWTs, and GitHub tokens. `redact_dict()` masks by sensitive key name recursively. Applied at: the streamed console line pump (`tools/console.py:_pump`), all Langfuse payloads (`_clean`), and context-graph writes (`redact`/`redact_dict`).

**Sensitive Terraform outputs** never leave the boundary: `TerraformRunner.output()` returns only non-sensitive outputs and the *names* of sensitive ones; the actual value is read via raw `terraform output -raw` **only** by the one-time reveal endpoint. This is a genuinely good design.

**Gaps:**
- The **plan JSON is captured raw (unredacted)** on purpose (`_capture_json`) so the redaction line-pump doesn't corrupt it — safe because it's reduced to addresses/actions, but if a diff ever carried a sensitive attribute value in `after`, it would be persisted in `runs.plan_json`. Today the modules keep secrets in sensitive outputs (not plan diffs), so this holds — but it's an invariant worth a test.
- **`Message.content` and `runs.outcome` are not redaction-scanned** before DB persistence — they rely on the agents never putting secrets there (cards route secrets to reveal-only). A future agent that echoes a secret into `answer` would persist it. Defense-in-depth would run `redact` on `answer` before `_persist_result`.
- Confidentiality classifier (`security/confidentiality.py`) is a crude heuristic (regex weights); it flags "production"/"iam"/emails, which inflates the badge but is harmless.

## 8.3 Auth (Keycloak OIDC) + RBAC enforcement points

**Auth.** Real OIDC (`integrations/keycloak.py`): password grant (form) + Auth-Code/PKCE (SSO), JWKS validation with kid-rotation refresh, **dual-issuer acceptance** (internal `keycloak:8080` for password-grant tokens + browser `localhost:8080` for SSO tokens), server-side sessions in Redis with proactive refresh, logout revokes the refresh token. `get_current_user` (`security/deps.py`) validates on every request and refreshes within 30s of expiry. This is correct and careful.

**RBAC model** (`security/rbac.py`): 8 realm roles → tiers. Approvers = {platform-admin, org-admin, cloud-architect}; initiators add {devops-engineer, sre, developer}; read-only = {auditor, read-only}. `can_execute == can_approve`.

**Enforcement points — where roles are actually checked:**

| Surface | Guard | Correct? |
|---------|-------|----------|
| All authed endpoints | `get_current_user` (401 if no session) | ✅ |
| `POST /approvals/{id}` | `Depends(require_approver)` — 403 non-approver | ✅ (tested) |
| `POST /knowledge/ingest` | `Depends(require_initiator)` | ✅ |
| Approve/Reject buttons (UI) | `user.can_approve` gate | ✅ (mirror only) |
| **`POST /runs/{id}/credentials`** (reveal private key/password) | **`get_current_user` only** | ❌ **hole** — a read-only auditor can reveal any run's secret (`api/artifacts.py:209`) |
| `GET /sessions/{id}/messages` | `get_current_user`, **no owner/org check** | ❌ any authed user reads any session |
| `GET /runs/{id}`, `GET /runs/{id}/{tab}` | `get_current_user`, no owner check | ❌ any authed user reads any run's plan/logs/approvals |
| `GET /chat/stream/{id}` | `get_current_user`, no owner check | ❌ any authed user attaches to any run stream |
| `POST /chat` (initiate a change) | `get_current_user` only — **not** `require_initiator` | ⚠ a read-only role can start a run (it can't approve, so it can't apply — but it shouldn't initiate) |
| Per-tool RBAC (agent layer) | **none** — tools don't receive the user | ❌ `CLAUDE.md` claims "RBAC re-checked at every side-effecting tool"; not implemented |

**Bottom line:** the *approval* gate — the one that authorizes real mutation — is correctly RBAC'd, and the durable interrupt makes it unbypassable. But the **read/reveal surface is under-authorized**, and per-tool RBAC + `/chat` initiator-gating are missing. With a single seeded org this hasn't bitten yet; it becomes a real cross-tenant leak the moment there's a second org (see [07 §7.5](07_datastores.md#75-consistency--coupling-risks-evidence) and [09](09_problems.md)).

**Approval integrity extras:** approvals are immutable (insert-only Approval rows + `_ensure_open` on the graph). There is **no 4-eyes check** — the same user who initiated a run can approve it (nothing compares approver to initiator; and initiator isn't even recorded on the run). For a governance product this is a notable omission.
