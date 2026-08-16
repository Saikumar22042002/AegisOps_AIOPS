"""Chat (SSE) + runs + approvals.

POST /chat            run the graph, streaming step/token/analysis/reference/confidentiality/
                      console/interrupt events; ends at `done` or at an approval `interrupt`.
POST /approvals/{id}  RBAC-gated; resumes the checkpointed graph (approve→apply/destroy streamed,
                      reject→halt) as an SSE stream of the continuation.
GET  /chat/stream/{id} reconnect/replay (Last-Event-ID) for an in-flight run.
GET  /runs/{id}        full run state.

GW-1: the run-driving core is factored into `prepare_run` / `build_drive` /
`resolve_approval_core`, and the HTTP routes below are thin callers of them. A messaging
gateway (`app/gateways/`) calls the SAME three functions, so a Telegram turn and a browser turn
are the same code — there is no second, laxer path to a plan or an apply. (Same reasoning as
waku's dashboard, where the non-streaming `chat()` drives `chat_stream` rather than
reimplementing it, because the two copies had already drifted.)
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

# P0/F-10: module-level import — the approval-wait metric block below uses `select`;
# a function-local-only import left it as a latent NameError swallowed by its
# try/except, so aegisops_approval_wait_seconds never recorded a single observation.
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

from ..agents.events import DONE, RunChannel, create_channel, get_channel
from ..agents.runner import run_graph
from ..agents.supervisor import get_supervisor
from ..db import repositories as repo
from ..db.models import Message, Run, RunStep, Session
from ..db.session import session_scope
from ..integrations import usage_ledger
from ..integrations.gemini import set_run_model
from ..llm import catalog as llm_catalog
from ..llm.errors import ModelError as LlmModelError
from ..logging_conf import bind_correlation, get_logger
from ..metrics import AGENT_RUNS, APPROVAL_WAIT
from ..ratelimit import limiter
from ..schemas.auth import User
from ..security.deps import authorize_run, get_current_user, require_approver, require_initiator
from ..settings import Settings, get_settings

log = get_logger(__name__)
router = APIRouter(tags=["chat"])


class ChatContext(BaseModel):
    org: str | None = None
    env: str | None = "Production"
    cloud: str | None = None  # U4: no silent AWS default; None ⇒ resolve_cloud asks if ambiguous
    region: str | None = "us-east-1"
    role: str | None = None


class ChatRequest(BaseModel):
    sessionId: str | None = None
    message: str
    model: str | None = None
    context: ChatContext = ChatContext()


class ApprovalRequest(BaseModel):
    decision: str  # approved | rejected
    rationale: str | None = None


async def iter_events(channel: RunChannel, replay_after: Any = 0):
    """Yield a run's raw `{"id","event","data"}` frames: replay first, then live, de-duped.

    The ONE consumer contract. `_sse` frames these for the browser; a messaging gateway renders
    the same frames into a chat — so a Telegram turn cannot see a different event stream than
    the web UI does.
    """
    # Track ids replayed from history so an event that is both in the ring buffer and still
    # pending in the queue (e.g. the leading `run` event emitted before this consumer starts)
    # is delivered exactly once, never duplicated.
    seen: set = set()
    for past in channel.replay_after(replay_after):
        seen.add(past["id"])
        yield past
    while True:
        item = await channel.queue.get()
        if item is DONE:
            break
        if item["id"] in seen:
            continue
        yield item


async def _sse(channel: RunChannel, replay_after: Any = 0):
    async for item in iter_events(channel, replay_after):
        yield {"event": item["event"], "data": json.dumps(item["data"]), "id": str(item["id"])}


async def _active_run_counts(org_id: str, user_id: str | None) -> tuple[int, int]:
    """PR-2a: count ACTIVE runs from the EXISTING liveness truth — a non-terminal
    runs.status AND a fresh run:<id>:hb heartbeat (BINDING: never a separate counter that
    could drift or lock an org out after a crash; heartbeat-derived counts self-heal like
    the reconciler). awaiting_approval does NOT count — it may legitimately wait days and
    holds no worker/subprocess. Returns (org_active, user_active)."""
    from ..agents.supervisor import hb_key
    from ..cache.redis import get_redis

    redis = get_redis()
    org_active = user_active = 0
    async with session_scope() as s:
        rows = (await s.execute(
            select(Run.id, Run.initiated_by).where(
                Run.org_id == uuid.UUID(org_id),
                # P0/D5: "applying" removed — the status is written by nothing at HEAD
                # (verified: zero writers; equivalence protocol in Redesign/09-review §10).
                Run.status.in_(("running",))))).all()
    for rid, initiated_by in rows:
        try:
            if not await redis.exists(hb_key(str(rid))):
                continue                     # stale row (crashed worker) — not truly active
        except Exception:                    # noqa: BLE001 — unreachable heartbeat ⇒ not active
            continue
        org_active += 1
        if user_id and str(initiated_by) == user_id:
            user_active += 1
    return org_active, user_active


async def _force_terminal(run_id: str, message: str) -> None:
    """B5 backstop: guarantee a run reaches a terminal state even if the normal persist path
    threw. A direct, self-guarded status write — the reconciler (B3) is the outer backstop, but
    this closes the common case (an exception inside `_drive`/`_persist_result`) immediately so a
    run is never left stuck in `running`."""
    try:
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            if run and run.status == "running":  # P0/D5: "applying" removed (no writers)
                run.status = "failed"
                run.outcome = {"status": "failed", "error": message[:500]}
                run.ended_at = datetime.now(UTC)  # P0/D7: run duration persisted
    except Exception as exc:  # noqa: BLE001 — last-ditch; the reconciler will catch what this can't
        log.error("chat.force_terminal_failed", run_id=run_id, error=str(exc))
    _cleanup_terminal_plan_files(run_id)
    await _release_cancel(run_id)


async def _mark_cancelled(run_id: str, message: str) -> None:
    """PR-3: set the run terminal `cancelled` (only from a non-terminal state — never
    overwrite an already-final status), clean the plan file, release the cancel flag."""
    try:
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            if run and run.status in ("running", "awaiting_approval"):  # P0/D5: no "applying"
                run.status = "cancelled"
                run.outcome = {"status": "cancelled", "note": message[:500]}
                run.ended_at = datetime.now(UTC)  # P0/D7
    except Exception as exc:  # noqa: BLE001
        log.error("chat.mark_cancelled_failed", run_id=run_id, error=str(exc))
    _cleanup_terminal_plan_files(run_id)
    await _release_cancel(run_id)


async def _release_cancel(run_id: str) -> None:
    try:
        from ..agents.supervisor import clear_cancel
        await clear_cancel(run_id)
    except Exception:  # noqa: BLE001
        pass


def _cleanup_terminal_plan_files(run_id: str) -> None:
    """PR-1a: a terminal run's .tfplan is deleted — the reviewable record persists in
    runs.plan_json. Best-effort; the reconciler's stray sweep is the backstop."""
    try:
        from ..settings import get_settings
        from ..tools.terraform import remove_run_plan_files
        remove_run_plan_files(get_settings(), run_id)
    except Exception as exc:  # noqa: BLE001 — hygiene must never break the persist path
        log.warning("chat.plan_cleanup_failed", run_id=run_id, error=str(exc))


async def _persist_result(run_id: str, session_id: str, org_id: str, state: dict, status_: str) -> str:
    """Persist the assistant message + run state; returns the assistant message id."""
    from ..security.redaction import redact, redact_dict

    # S4: redaction backstop — nothing persisted to messages.content / runs.outcome may
    # carry a secret, even if a future agent echoes one into its answer. Console/graph/
    # Langfuse already redact; this closes the last persistence path (P20).
    answer = redact(state.get("answer", "") or "")
    outcome = state.get("outcome")
    if isinstance(outcome, dict):
        outcome = redact_dict(outcome)
    async with session_scope() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        if run:
            run.status = status_
            if status_ in ("completed", "failed", "cancelled"):
                run.ended_at = datetime.now(UTC)  # P0/D7: duration persisted
            run.intent = state.get("intent")
            run.confidence = state.get("intent_confidence")
            run.routing_reason = state.get("routing_reason")
            run.domain = state.get("domain")
            run.workflow = state.get("workflow")
            run.workflow_version = state.get("workflow_version")
            run.mode = state.get("execution_mode", run.mode)
            run.plan_json = state.get("plan_json")
            run.input_json = state.get("parsed_inputs")
            run.outcome = outcome
            run.context_id = state.get("context_id") or run_id
            run.snow_id = state.get("snow_id")
        conf = state.get("confidentiality", {})
        msg = Message(
            org_id=uuid.UUID(org_id), session_id=uuid.UUID(session_id), role="assistant",
            content=answer, confidentiality_level=conf.get("level"),
            confidentiality_score=conf.get("score"), context_id=run_id,
            trace_id=state.get("trace_id"), run_id=uuid.UUID(run_id),
            analysis={"references": state.get("references", []), "reasoning": state.get("reasoning_cards", []),
                      "param_request": state.get("param_request")},
        )
        s.add(msg)
        await s.flush()
        msg_id = str(msg.id)
    # M2: embed the assistant message for semantic recall (best-effort, background; NULL without
    # a Gemini key → keyword-recall fallback). Fired after commit so it re-fetches a persisted row.
    from ..agents import memory as _memory
    asyncio.create_task(_memory.embed_message(msg_id, answer, get_settings()))
    # PR-1a: terminal runs leave no plan file behind (awaiting_approval keeps its plan —
    # the approved apply still needs it). PR-3: release the cancel flag at any terminal state.
    if status_ in ("completed", "failed", "cancelled"):
        _cleanup_terminal_plan_files(run_id)
        await _release_cancel(run_id)
    return msg_id


@dataclass
class PreparedRun:
    """Everything `build_drive` needs, produced by `prepare_run` (which owns every refusal)."""

    run_id: str
    session_id: str
    org_id: str
    resolved_model: str
    env: str | None
    source: str
    initiator_user_id: str | None
    initial: dict = field(default_factory=dict)


async def prepare_run(*, user: User, message: str, context: ChatContext,
                      session_id: str | None, model: str | None, settings: Settings,
                      source: str = "web") -> PreparedRun:
    """Validate + persist the start of a run. Raises HTTPException for every refusal.

    This is the ONE place a run is admitted: model validation (U3), active-run limits (PR-2a),
    org-scoped session resolution (S0), the user message row, and the `Run` governance row (A5).
    `source` tags which gateway initiated it. Callers must already have enforced initiator RBAC
    (the HTTP route via `require_initiator`; the gateway via the bound user's `can_initiate`).
    """
    # U3: the requested model is validated against the real provider catalog up front. An
    # unknown model fails loudly (400) instead of being silently ignored; the resolved id is
    # bound to this run so the model the operator picked is the model the run actually uses.
    try:
        cat = llm_catalog.load()
        info = cat.model(model) if model else cat.model(cat.purposes["general"].model)
        if not cat.provider_configured(info.provider, settings):
            raise LlmModelError(
                "invalid_request",
                f"model {info.id!r} needs provider {info.provider!r}, which has no "
                f"credentials configured on this deployment")
        resolved_model = info.id
    except LlmModelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
        owner_id = uuid.UUID(user.user_id) if user.user_id else None
        # PR-2a: refuse a new run BEFORE persisting anything (no row leaked) when the org or
        # user is at its ACTIVE-run limit. Terraform processes are heavy; queueing isn't
        # supported yet, so the refusal is honest. Counts derive from the liveness truth.
        org_active, user_active = await _active_run_counts(org_id, user.user_id)
        if org_active >= settings.max_active_runs_per_org:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"{org_active} runs already in progress for your org "
                                f"(limit {settings.max_active_runs_per_org}). Queuing isn't "
                                "supported yet — retry when one completes.")
        if user.user_id and user_active >= settings.max_active_runs_per_user:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                f"You have {user_active} runs in progress "
                                f"(limit {settings.max_active_runs_per_user}). Retry when one "
                                "completes.")
        if session_id:
            # The caller may only continue a session that exists in THEIR org (S0).
            try:
                sess = await s.get(Session, uuid.UUID(session_id))
            except ValueError:
                raise HTTPException(404, "session not found") from None
            if not sess or sess.org_id != org.id:
                raise HTTPException(404, "session not found")
            resolved_session_id = session_id
        else:
            sess = Session(org_id=org.id, user_id=owner_id,
                           title=message[:80] or "New conversation", source=source)
            s.add(sess)
            await s.flush()
            resolved_session_id = str(sess.id)
        user_msg = Message(org_id=org.id, session_id=uuid.UUID(resolved_session_id), role="user",
                           content=message)
        s.add(user_msg)
        run = Run(org_id=org.id, session_id=uuid.UUID(resolved_session_id), status="running",
                  mode=settings.default_execution_mode,
                  initiated_by=owner_id, env=context.env,  # A5: governance facts
                  source=source)                           # GW-1: channel provenance
        s.add(run)
        await s.flush()
        run_id = str(run.id)
        user_msg_id = str(user_msg.id)
    # M2: embed the user message for semantic recall (best-effort, background).
    from ..agents import memory as _memory
    asyncio.create_task(_memory.embed_message(user_msg_id, message, settings))

    bind_correlation(run_id=run_id, session_id=resolved_session_id)
    user_ctx = {**user.model_dump(), "env": context.env, "cloud": context.cloud,
                "region": context.region, "source": source}
    initial = {
        "message": message, "org_id": org_id, "user": user_ctx,
        "session_id": resolved_session_id, "run_id": run_id, "context_id": run_id,
        "trace_id": run_id, "messages": [HumanMessage(content=message)],
    }
    return PreparedRun(run_id=run_id, session_id=resolved_session_id, org_id=org_id,
                       resolved_model=resolved_model, env=context.env, source=source,
                       initiator_user_id=user.user_id, initial=initial)


def build_drive(prepared: PreparedRun, channel: RunChannel) -> Callable[[], Awaitable[None]]:
    """The run drive, identical for every caller. Hand the result to `get_supervisor().run`."""

    run_id, session_id, org_id = prepared.run_id, prepared.session_id, prepared.org_id

    async def _drive() -> None:
        from ..agents.events import Emitter
        set_run_model(prepared.resolved_model)  # U3: bind the model for this run's async context
        usage_ledger.bind_run(run_id, org_id)   # P0: ledger accounting attribution (run/org)
        emitter = Emitter(channel)
        try:
            # Lead with the run identity so the client binds its live artifact panel to THIS
            # run's id (and learns the real session id) from the very first event — before any
            # step/token. This is what lets the timeline update live and lets the persisted
            # message link to its run from the moment it starts.
            await emitter.run({"runId": run_id, "sessionId": session_id})
            res = await run_graph(run_id, channel, initial=prepared.initial)
            state = res["state"]
            error = res.get("error")
            # A graph failure must be persisted as FAILED with a real message — never as an
            # empty "completed" run (that produced the "Agent Agent / Classified → intent"
            # placeholder timeline of screenshots 15/16). (Phase 7 / BUG-03.)
            status_ = "failed" if error else ("awaiting_approval" if res["interrupted"] else "completed")
            # PR-3c: the exec loop can finish with an honest cancelled outcome — that is a
            # terminal `cancelled`, not `completed`.
            if not error and (state.get("outcome") or {}).get("status") == "cancelled":
                status_ = "cancelled"
            if error and not state.get("answer"):
                state = {**state, "answer": f"⚠️ This run failed unexpectedly: {error}. "
                                            "Nothing was changed — please send that again.",
                         "outcome": state.get("outcome") or {"status": "failed", "error": error}}
            msg_id = await _persist_result(run_id, session_id, org_id, state, status_)
            AGENT_RUNS.labels(domain=state.get("domain", "general"), workflow=state.get("workflow", "-"),
                              status=status_, env=prepared.env or "na").inc()
            if res["interrupted"]:
                # GW-1: a run parked at the gate is pushed to every LINKED, ELIGIBLE approver's
                # channel, whichever gateway started it — so a browser-initiated change is
                # approvable from a phone and vice versa. Best-effort by contract.
                await _notify_gateways_awaiting_approval(prepared, state)
            else:
                await emitter.done({
                    "messageId": msg_id, "runId": run_id, "traceId": run_id,
                    "contextId": state.get("context_id", run_id), "snowId": state.get("snow_id"),
                    "outcome": state.get("outcome") or ({"status": "failed", "error": error} if error
                                                        else {"status": "completed"}),
                })
        except asyncio.CancelledError:
            # PR-3a: a pre-approval cancel cancels the live drive → terminal `cancelled`
            # ("nothing was changed"), plan file cleaned, idempotency naturally released
            # (the apply claim was never taken). Re-raised so the task truly unwinds.
            log.info("chat.drive_cancelled", run_id=run_id)
            await _mark_cancelled(run_id, "cancelled before any change was applied")
            raise
        except Exception as exc:  # noqa: BLE001 — B5: never leave the run stuck in `running`
            log.error("chat.drive_failed", run_id=run_id, error=str(exc))
            await _force_terminal(run_id, f"run driver failed: {exc}")
            try:
                await emitter.error(f"This run failed unexpectedly: {exc}", code="drive_error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            await channel.close()

    return _drive


async def _notify_gateways_awaiting_approval(prepared: PreparedRun, state: dict) -> None:
    """Hand an awaiting-approval run to the gateway push layer. Best-effort by contract: a
    messaging problem must never change what happens to the run."""
    try:
        from ..gateways import notify as gw_notify
        await gw_notify.approval_pending(
            run_id=prepared.run_id, org_id=prepared.org_id,
            initiator_username=(state.get("user") or {}).get("username"),
            interrupt_payload=state.get("interrupt_payload") or {})
    except Exception as exc:  # noqa: BLE001 — a push failure must never affect the run
        log.warning("chat.gateway_notify_failed", run_id=prepared.run_id, error=str(exc))


@router.post("/chat")
async def chat(body: ChatRequest, request: Request, user: User = Depends(require_initiator),
               settings: Settings = Depends(get_settings)):
    # S3: read-only roles (auditor/read-only) cannot initiate a run — they can still view
    # (GET endpoints stay on get_current_user). require_initiator → 403 with a clear message.
    prepared = await prepare_run(user=user, message=body.message, context=body.context,
                                 session_id=body.sessionId, model=body.model,
                                 settings=settings, source="web")
    channel = create_channel(prepared.run_id)
    # B2: tracked task + heartbeat (was fire-and-forget)
    get_supervisor().run(prepared.run_id, build_drive(prepared, channel))
    return EventSourceResponse(_sse(channel))


def _record_approval_wait(domain: str | None, decision: str, started_at) -> None:
    """O3: observe the real human-approval wait (seconds) labeled by domain + decision.

    `started_at` is the approval step's start (when the run paused at the gate); None for a
    legacy run with no recorded approval step, in which case there is nothing honest to record."""
    if not started_at:
        return
    from datetime import datetime
    wait = (datetime.now(UTC) - started_at).total_seconds()
    APPROVAL_WAIT.labels(domain=domain or "unknown", decision=decision).observe(max(wait, 0.0))


async def resolve_approval_core(run_id: str, *, decision: str, rationale: str | None,
                                user: User, settings: Settings) -> tuple[Any, Any]:
    """Record + drive an approval decision. Returns (channel, continuation_cursor).

    Every refusal is an HTTPException raised from here, so the HTTP route and a messaging
    gateway refuse identically: unknown decision (400), cross-org run (404),
    not-awaiting-approval (409), concurrent decision in flight (409). RBAC
    (`can_approve`) is the caller's guard — the HTTP route via `require_approver`, a gateway by
    re-checking the bound user at click time. Approval is single-user human-in-the-loop:
    the initiating human is an authorized approver of their own plan (initiator == approver).
    """
    if decision not in {"approved", "rejected"}:
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")
    if not user.can_approve:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "Approval requires Cloud Architect, Org Admin, or Platform Admin.")
    async with session_scope() as s:
        try:
            run = await s.get(Run, uuid.UUID(run_id))
        except ValueError:
            raise HTTPException(404, "run not found") from None
        # S0 org predicate: a run outside the approver's org does not exist for them.
        if not run or (user.org_id and str(run.org_id) != user.org_id):
            raise HTTPException(404, "run not found")
        if run.status != "awaiting_approval":
            raise HTTPException(status.HTTP_409_CONFLICT, "run is not awaiting approval")
        org_id, session_id = str(run.org_id), str(run.session_id)
        # O3: record the real human-approval wait — from when the run paused at the gate (the
        # approval step's start) to this decision. Labeled by domain + decision. Best-effort.
        try:
            step = (await s.execute(
                select(RunStep).where(RunStep.run_id == run.id, RunStep.name == "approval")
            )).scalar_one_or_none()
            _record_approval_wait(run.domain, decision, step.started_at if step else None)
        except Exception as exc:  # noqa: BLE001 — metrics never block an approval
            log.warning("approval.wait_metric_failed", run_id=run_id, error=str(exc))

    # A1 endpoint guard: reject a second /approvals for this run while a prior decision is
    # still being driven (the run stays `awaiting_approval` in the DB until the drive ends,
    # so the status check above cannot catch a concurrent double-click). An NX lock closes
    # that window; idempotency wait-or-abort is the backstop inside the execute node.
    from ..cache.redis import get_redis

    inflight_key = f"approval:inflight:{run_id}"
    if not await get_redis().set(inflight_key, user.username, nx=True, ex=900):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "this run's approval is already being processed")

    channel = create_channel(run_id)  # fresh channel for the continuation stream
    # STAB P0-3: tail the stream from NOW. On the redis bus the run's stream key still
    # holds the original turn's frames ending in ITS __eos__ — a from-zero consumer
    # replays the plan turn and stops at that old marker, so the browser never receives
    # the apply progress or `done` (the exact live "approve then silence" on the
    # multi-worker posture). The cursor is captured BEFORE the drive starts, so no
    # continuation frame can be missed. Memory-mode channels are fresh (cursor 0).
    continuation_cursor = await channel.current_cursor()
    resume_value = {"decision": decision, "user": user.username,
                    "role": user.display_roles[0] if user.display_roles else "", "rationale": rationale,
                    "can_execute": user.can_execute,  # S5: carry the approver's execute capability
                    "email": user.email}              # P17: notify the APPROVER, never the sender

    async def _drive():
        from ..agents.events import Emitter
        usage_ledger.bind_run(run_id, org_id)  # P0: continuation spend attributed to the run
        emitter = Emitter(channel)
        try:
            await emitter.run({"runId": run_id, "sessionId": session_id})
            res = await run_graph(run_id, channel, resume=resume_value)
            state = res["state"]
            error = res.get("error")
            status_ = "failed" if error else "completed"
            if error and not state.get("answer"):
                state = {**state, "answer": f"⚠️ The continuation failed unexpectedly: {error}.",
                         "outcome": state.get("outcome") or {"status": "failed", "error": error}}
            msg_id = await _persist_result(run_id, session_id, org_id, state, status_)
            await emitter.done({
                "messageId": msg_id, "runId": run_id, "traceId": run_id,
                "contextId": state.get("context_id", run_id), "snowId": state.get("snow_id"),
                "outcome": state.get("outcome") or ({"status": "failed", "error": error} if error
                                                    else {"status": decision}),
            })
        except Exception as exc:  # noqa: BLE001 — B5: a failed continuation must still terminate
            log.error("chat.approval_drive_failed", run_id=run_id, error=str(exc))
            await _force_terminal(run_id, f"approval continuation failed: {exc}")
            try:
                await emitter.error(f"The continuation failed unexpectedly: {exc}", code="drive_error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            await get_redis().delete(inflight_key)  # A1: release the endpoint guard
            await channel.close()

    get_supervisor().run(run_id, _drive)  # B2: tracked task + heartbeat (was fire-and-forget)
    return channel, continuation_cursor


@router.post("/approvals/{run_id}")
async def resolve_approval(run_id: str, body: ApprovalRequest,
                           user: User = Depends(require_approver),
                           settings: Settings = Depends(get_settings)):
    channel, cursor = await resolve_approval_core(
        run_id, decision=body.decision, rationale=body.rationale, user=user, settings=settings)
    return EventSourceResponse(_sse(channel, replay_after=cursor))


@router.get("/chat/stream/{run_id}")
async def chat_stream(run_id: str, user: User = Depends(get_current_user),
                      last_event_id: str | None = Header(default=None)):
    # S2: authorize BEFORE attaching to the stream — a cross-org run id must 404 whether
    # or not a live channel exists for it.
    async with session_scope() as s:
        try:
            run = await s.get(Run, uuid.UUID(run_id))
        except ValueError:
            raise HTTPException(404, "run not found") from None
        authorize_run(run, user)
    channel = get_channel(run_id)
    if not channel:
        raise HTTPException(404, "no active stream for this run")
    # B1: in redis mode any worker can serve the stream, but a run whose stream never started or
    # already evicted has nothing to attach to → 404 (parity with memory mode's missing channel).
    if hasattr(channel, "exists") and not await channel.exists():
        raise HTTPException(404, "no active stream for this run")
    # Cursor: memory ids are ints; redis ids are stream ids. Each channel's replay_after coerces.
    after: Any = last_event_id if last_event_id else 0
    return EventSourceResponse(_sse(channel, replay_after=after))


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: User = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        try:
            run = await s.get(Run, uuid.UUID(run_id))
        except ValueError:
            raise HTTPException(404, "run not found") from None
        authorize_run(run, user)  # S2: cross-org run reads are 404
        return {
            "id": str(run.id), "intent": run.intent, "confidence": run.confidence,
            "domain": run.domain, "workflow": run.workflow, "version": run.workflow_version,
            "mode": run.mode, "status": run.status, "plan_json": run.plan_json,
            "input_json": run.input_json, "outcome": run.outcome, "snow_id": run.snow_id,
            "context_id": run.context_id,
            # GW-1: a `?run=<id>` deep link (the link a chat channel sends when it withholds or
            # truncates an answer) must be able to find the conversation the run belongs to.
            # Same org-authorized payload — `authorize_run` above already gated it.
            "session_id": str(run.session_id) if run.session_id else None,
            "source": run.source,
        }


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: User = Depends(get_current_user)) -> dict:
    """PR-3: cooperative cancel. Pre-approval/executing → the drive stops at the next safe
    boundary and the run goes terminal `cancelled` (never a mid-apply kill). Authz: the
    initiator OR an approver in the org (audited like approvals). Terminal runs are a no-op."""
    from ..agents.supervisor import request_cancel

    async with session_scope() as s:
        try:
            run = await s.get(Run, uuid.UUID(run_id))
        except ValueError:
            raise HTTPException(404, "run not found") from None
        authorize_run(run, user)  # S2: cross-org is 404
        # initiator or approver only
        is_initiator = run.initiated_by is not None and str(run.initiated_by) == (user.user_id or "")
        if not (is_initiator or user.can_execute):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "only the run's initiator or an approver can cancel it")
        if run.status in ("completed", "failed", "cancelled"):
            return {"id": run_id, "status": run.status, "note": "already terminal — no-op"}
        current = run.status

    log.info("chat.cancel_requested", run_id=run_id, by=user.username, from_status=current)
    await request_cancel(run_id)
    # awaiting_approval holds no live drive — flip it terminal directly (equivalent to Reject
    # for cancel purposes). Executing runs are flipped by their own drive at the next boundary;
    # the reconciler is the backstop if that worker is already gone.
    if current == "awaiting_approval":
        await _mark_cancelled(run_id, "cancelled while awaiting approval")
    return {"id": run_id, "status": "cancelling",
            "note": "cancellation requested; the run will stop at the next safe point "
                    "(never mid-apply)."}


# O3: exempt the SSE endpoints from the per-IP rate limit. We register the exemption by NAME
# rather than via @limiter.exempt, because that decorator wraps the endpoint in a
# `(*args, **kwargs)` shim FastAPI cannot introspect — the ChatRequest body param is lost and
# every POST /chat 422s. SlowAPIMiddleware checks `f"{fn.__module__}.{fn.__name__}"` against
# `limiter._exempt_routes`, so registering the real (unwrapped) function names has the same
# effect while leaving the signatures — and request validation — intact.
for _sse_endpoint in (chat, chat_stream):
    limiter._exempt_routes.add(f"{_sse_endpoint.__module__}.{_sse_endpoint.__name__}")
