"""Chat (SSE) + runs + approvals + interactive input.

POST /chat            run the graph, streaming step/token/analysis/reference/confidentiality/
                      console/interrupt events; ends at `done` or at an approval `interrupt`.
POST /approvals/{id}  RBAC-gated; resumes the checkpointed graph (approve→apply/destroy streamed,
                      reject→halt) as an SSE stream of the continuation.
GET  /chat/stream/{id} reconnect/replay (Last-Event-ID) for an in-flight run.
GET  /runs/{id}        full run state. POST /runs/{id}/input  answer an interactive prompt.
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from langchain_core.messages import HumanMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..agents.events import DONE, RunChannel, create_channel, get_channel
from ..agents.runner import run_graph
from ..db import repositories as repo
from ..db.models import Message, Run, Session
from ..db.session import session_scope
from ..logging_conf import bind_correlation, get_logger
from ..metrics import AGENT_RUNS
from ..schemas.auth import User
from ..security.deps import authorize_run, get_current_user, require_approver, require_initiator
from ..settings import Settings, get_settings

log = get_logger(__name__)
router = APIRouter(tags=["chat"])


class ChatContext(BaseModel):
    org: str | None = None
    env: str | None = "Production"
    cloud: str | None = "AWS"
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


async def _sse(channel: RunChannel, replay_after: int = 0):
    # Track ids replayed from history so an event that is both in the ring buffer and still
    # pending in the queue (e.g. the leading `run` event emitted before this consumer starts)
    # is delivered exactly once, never duplicated.
    seen: set[int] = set()
    for past in channel.replay_after(replay_after):
        seen.add(past["id"])
        yield {"event": past["event"], "data": json.dumps(past["data"]), "id": str(past["id"])}
    while True:
        item = await channel.queue.get()
        if item is DONE:
            break
        if item["id"] in seen:
            continue
        yield {"event": item["event"], "data": json.dumps(item["data"]), "id": str(item["id"])}


async def _force_terminal(run_id: str, message: str) -> None:
    """B5 backstop: guarantee a run reaches a terminal state even if the normal persist path
    threw. A direct, self-guarded status write — the reconciler (B3) is the outer backstop, but
    this closes the common case (an exception inside `_drive`/`_persist_result`) immediately so a
    run is never left stuck in `running`."""
    try:
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            if run and run.status in ("running", "applying"):
                run.status = "failed"
                run.outcome = {"status": "failed", "error": message[:500]}
    except Exception as exc:  # noqa: BLE001 — last-ditch; the reconciler will catch what this can't
        log.error("chat.force_terminal_failed", run_id=run_id, error=str(exc))


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
        return str(msg.id)


@router.post("/chat")
async def chat(body: ChatRequest, request: Request, user: User = Depends(require_initiator),
               settings: Settings = Depends(get_settings)):
    # S3: read-only roles (auditor/read-only) cannot initiate a run — they can still view
    # (GET endpoints stay on get_current_user). require_initiator → 403 with a clear message.
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
        owner_id = uuid.UUID(user.user_id) if user.user_id else None
        if body.sessionId:
            # The caller may only continue a session that exists in THEIR org (S0).
            try:
                sess = await s.get(Session, uuid.UUID(body.sessionId))
            except ValueError:
                raise HTTPException(404, "session not found") from None
            if not sess or sess.org_id != org.id:
                raise HTTPException(404, "session not found")
            session_id = body.sessionId
        else:
            sess = Session(org_id=org.id, user_id=owner_id,
                           title=body.message[:80] or "New conversation")
            s.add(sess)
            await s.flush()
            session_id = str(sess.id)
        s.add(Message(org_id=org.id, session_id=uuid.UUID(session_id), role="user", content=body.message))
        run = Run(org_id=org.id, session_id=uuid.UUID(session_id), status="running",
                  mode=settings.default_execution_mode,
                  initiated_by=owner_id, env=body.context.env)  # A5: governance facts
        s.add(run)
        await s.flush()
        run_id = str(run.id)

    bind_correlation(run_id=run_id, session_id=session_id)
    channel = create_channel(run_id)
    user_ctx = {**user.model_dump(), "env": body.context.env, "cloud": body.context.cloud,
                "region": body.context.region}
    initial = {
        "message": body.message, "org_id": org_id, "user": user_ctx,
        "session_id": session_id, "run_id": run_id, "context_id": run_id, "trace_id": run_id,
        "messages": [HumanMessage(content=body.message)],
    }

    async def _drive():
        from ..agents.events import Emitter
        emitter = Emitter(channel)
        try:
            # Lead with the run identity so the client binds its live artifact panel to THIS
            # run's id (and learns the real session id) from the very first event — before any
            # step/token. This is what lets the timeline update live and lets the persisted
            # message link to its run from the moment it starts.
            await emitter.run({"runId": run_id, "sessionId": session_id})
            res = await run_graph(run_id, channel, initial=initial)
            state = res["state"]
            error = res.get("error")
            # A graph failure must be persisted as FAILED with a real message — never as an
            # empty "completed" run (that produced the "Agent Agent / Classified → intent"
            # placeholder timeline of screenshots 15/16). (Phase 7 / BUG-03.)
            status_ = "failed" if error else ("awaiting_approval" if res["interrupted"] else "completed")
            if error and not state.get("answer"):
                state = {**state, "answer": f"⚠️ This run failed unexpectedly: {error}. "
                                            "Nothing was changed — please send that again.",
                         "outcome": state.get("outcome") or {"status": "failed", "error": error}}
            msg_id = await _persist_result(run_id, session_id, org_id, state, status_)
            AGENT_RUNS.labels(domain=state.get("domain", "general"), workflow=state.get("workflow", "-"),
                              status=status_, env=body.context.env or "na").inc()
            if not res["interrupted"]:
                await emitter.done({
                    "messageId": msg_id, "runId": run_id, "traceId": run_id,
                    "contextId": state.get("context_id", run_id), "snowId": state.get("snow_id"),
                    "outcome": state.get("outcome") or ({"status": "failed", "error": error} if error
                                                        else {"status": "completed"}),
                })
        except Exception as exc:  # noqa: BLE001 — B5: never leave the run stuck in `running`
            log.error("chat.drive_failed", run_id=run_id, error=str(exc))
            await _force_terminal(run_id, f"run driver failed: {exc}")
            try:
                await emitter.error(f"This run failed unexpectedly: {exc}", code="drive_error")
            except Exception:  # noqa: BLE001
                pass
        finally:
            await channel.close()

    asyncio.create_task(_drive())
    return EventSourceResponse(_sse(channel))


@router.post("/approvals/{run_id}")
async def resolve_approval(run_id: str, body: ApprovalRequest,
                           user: User = Depends(require_approver), settings: Settings = Depends(get_settings)):
    if body.decision not in {"approved", "rejected"}:
        raise HTTPException(400, "decision must be 'approved' or 'rejected'")
    async with session_scope() as s:
        try:
            run = await s.get(Run, uuid.UUID(run_id))
        except ValueError:
            raise HTTPException(404, "run not found") from None
        # S0 org predicate: a run outside the approver's org does not exist for them.
        if not run or (user.org_id and str(run.org_id) != user.org_id):
            raise HTTPException(404, "run not found")
        # A5 4-eyes: the initiator of a Production change cannot approve it themselves.
        # Skipped for legacy runs with no recorded initiator (pre-A5 rows).
        if (settings.aegisops_four_eyes_for_production and (run.env or "").lower() == "production"
                and run.initiated_by and user.user_id and str(run.initiated_by) == user.user_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "Four-eyes policy: you initiated this Production change — "
                                "a different approver must review it.")
        if run.status != "awaiting_approval":
            raise HTTPException(status.HTTP_409_CONFLICT, "run is not awaiting approval")
        org_id, session_id = str(run.org_id), str(run.session_id)

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
    resume_value = {"decision": body.decision, "user": user.username,
                    "role": user.display_roles[0] if user.display_roles else "", "rationale": body.rationale,
                    "can_execute": user.can_execute}  # S5: carry the approver's execute capability

    async def _drive():
        from ..agents.events import Emitter
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
                                                    else {"status": body.decision}),
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

    asyncio.create_task(_drive())
    return EventSourceResponse(_sse(channel))


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
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0
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
        }


@router.post("/runs/{run_id}/input")
async def run_input(run_id: str, body: dict, user: User = Depends(get_current_user)) -> dict:
    """Answer an interactive console prompt (password/input). Value is masked, never logged."""
    from ..cache.redis import get_redis

    await get_redis().rpush(f"runinput:{run_id}", json.dumps({"value": body.get("value", "")}))
    return {"status": "received"}
