# AegisOps — Platform Workflow Documentation

> **What this is.** A code-grounded map of what AegisOps does *today*. Every claim cites
> `file:line` (or `file:function`) in the running codebase. Nothing here is aspirational; where
> a spec doc (`FIX.md`, `PROGRESS.md`) and the code disagreed while writing this, **the code
> won**. If you change the code, change the citation.
>
> Generated 2026-07-21 against `feature/cloudops-v1`. Line numbers are exact at that commit;
> treat them as "look here", not gospel — verify before quoting in a review.

## The doc set

| Doc | Covers |
|---|---|
| [00_architecture.md](00_architecture.md) | Split-trust whiteboard: every box → the file that implements it. Container/infra topology (compose services, volumes, ports, feature flags). |
| [01_request_lifecycle.md](01_request_lifecycle.md) | One chat message end to end: composer → `POST /chat` → auth/RBAC/tenancy → run/session → SSE → router → agent → approval → execute → verify → finalize → persist → render. Sequence diagrams per path. |
| [02_langgraph.md](02_langgraph.md) | Every node in `graph.py`, state channels, edges/routing, the approval interrupt/resume mechanics, durable checkpoint, timing wrapper, terminal-state guarantees. |
| [03_harness.md](03_harness.md) | The four pillars: **Memory** (the layers + retrieval paths), **Tools** (read-only registry, terraform runner, cloud readers, mutation boundary), **Tracing/Ops** (Langfuse tree, metrics, reconciler/supervisor, event bus, idempotency), **Evals** (what exists, what doesn't). |
| [04_frontend.md](04_frontend.md) | `store.ts` state, the SSE event vocabulary → reducer → render, per-message run binding, the artifact panel tabs, approval-card lifecycle + restoration, credential reveal/download, session restore. |
| [05_reads.md](05_reads.md) | Source-of-truth reads: for each question type, the resolution chain — classification → which store(s) → which functions → live-SDK reconcile → honesty statuses. |
| [06_catalog.md](06_catalog.md) | Every registered workflow module: key, cloud, creates-what, required/optional params + defaults, outputs, policy checks, day-2 caps, destroy semantics, DEP slots. |
| [07_devops_sre.md](07_devops_sre.md) | DevOps staged pipeline (dispatch → find → poll → conclusion) and SRE triage → PromQL signals → read-only investigation → decision matrix → gated remediation. |

## The one-paragraph mental model

A chat message is classified by a **Router** (`agents/router.py`) into one of five domains
(cloudops/devops/sre/knowledge/general). Read-only work and conversation run freely. Any
**mutation** is funneled through a LangGraph state machine (`agents/graph.py`) that pauses at a
durable **human-approval interrupt** (`agents/approval.py`) before a single Terraform action
runs — this is the split-trust boundary: the LLM plans, deterministic governed code applies.
The whole run streams to the browser over SSE (`api/chat.py` → `agents/events.py`), is traced
end to end in Langfuse under one trace id == run id (`integrations/langfuse_client.py`), and is
recoverable after a crash by a supervised runner + reconciler (`agents/supervisor.py`,
`agents/reconciler.py`). Cloud SDKs are **read-only** (discovery/verify only); Terraform is the
only thing that creates/modifies/destroys.

## Invariants the code enforces (cited in the docs)

1. **No mutation without a passed human-approval interrupt.** `agents/graph.py:103` routes
   `approval → execute` only on `approval_status == "approved"`; `agents/execute.py:21-29`
   re-asserts approver execute-capability and fails closed.
2. **Cloud SDKs never provision.** `tools/aws.py:1`, `tools/gcp.py:1`, `tools/azure.py:1` —
   read-only discovery/verify; the mutation path is Terraform (`tools/terraform.py`).
3. **A question is never a mutating action.** `agents/intent_guard.py:guard_classification`
   downgrades any question-shaped message to `action=read` and strips side-effect intents.
4. **A run always reaches a terminal state.** `api/chat.py:_force_terminal` + the `except`/
   `finally` in both drive closures + the reconciler's stranded-run sweep.
5. **Everything real, no mocks.** Failures surface loudly with a reason; no fabricated data.

See each doc for the enforcing lines.
