"""Sessions, messages, and feedback (org-scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..db import repositories as repo
from ..db.models import Feedback, Message, Session
from ..db.session import session_scope
from ..graph_db.context_graph import ContextGraph
from ..logging_conf import get_logger
from ..schemas.auth import User
from ..security.deps import get_current_user

log = get_logger(__name__)
router = APIRouter(tags=["sessions"])


class NewSession(BaseModel):
    title: str | None = "New conversation"


class RenameSession(BaseModel):
    title: str


class FeedbackRequest(BaseModel):
    messageId: str
    value: str  # up | down
    comment: str | None = None
    sensitive: bool = False


@router.get("/sessions")
async def list_sessions(user: User = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        rows = (await s.execute(
            select(Session).where(Session.org_id == org.id).order_by(Session.created_at.desc()).limit(100)
        )).scalars()
        return {"sessions": [{"id": str(r.id), "title": r.title, "status": r.status,
                              "created_at": r.created_at.isoformat()} for r in rows]}


@router.post("/sessions")
async def create_session(body: NewSession, user: User = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        sess = Session(org_id=org.id,
                       user_id=uuid.UUID(user.user_id) if user.user_id else None,
                       title=body.title or "New conversation")
        s.add(sess)
        await s.flush()
        return {"id": str(sess.id), "title": sess.title, "status": sess.status}


async def _org_session(s, session_id: str, user: User) -> Session:
    """Load a session iff it belongs to the caller's org; 404 otherwise (no enumeration)."""
    org = await repo.org_for(s, user)
    try:
        sess = await s.get(Session, uuid.UUID(session_id))
    except ValueError:
        raise HTTPException(404, "session not found") from None
    if not sess or sess.org_id != org.id:
        raise HTTPException(404, "session not found")
    return sess


@router.get("/sessions/{session_id}/messages")
async def session_messages(session_id: str, user: User = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        rows = (await s.execute(
            select(Message).where(Message.session_id == uuid.UUID(session_id)).order_by(Message.created_at)
        )).scalars()
        return {"messages": [{
            "id": str(m.id), "role": m.role, "content": m.content,
            "confidentiality": {"level": m.confidentiality_level, "score": m.confidentiality_score},
            "analysis": m.analysis, "run_id": str(m.run_id) if m.run_id else None,
            "created_at": m.created_at.isoformat(),
        } for m in rows]}


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: str, body: RenameSession, user: User = Depends(get_current_user)) -> dict:
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "title must not be empty")
    async with session_scope() as s:
        sess = await _org_session(s, session_id, user)
        sess.title = title[:300]
        return {"id": session_id, "title": sess.title, "status": sess.status}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: User = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        sess = await _org_session(s, session_id, user)
        # Messages cascade (relationship delete-orphan + FK ON DELETE CASCADE); runs detach (session_id -> NULL).
        await s.delete(sess)
    return {"id": session_id, "status": "deleted"}


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str, user: User = Depends(get_current_user)) -> dict:
    from datetime import datetime, timezone

    async with session_scope() as s:
        sess = await _org_session(s, session_id, user)
        sess.status = "closed"
        sess.closed_at = datetime.now(timezone.utc)
        return {"id": session_id, "status": "closed"}


@router.post("/feedback")
async def submit_feedback(body: FeedbackRequest, user: User = Depends(get_current_user)) -> dict:
    if body.value not in {"up", "down"}:
        raise HTTPException(400, "value must be 'up' or 'down'")
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        try:
            msg = await s.get(Message, uuid.UUID(body.messageId))
        except ValueError:
            raise HTTPException(404, "message not found") from None
        # S0 org predicate: a message outside the caller's org does not exist for them.
        if not msg or msg.org_id != org.id:
            raise HTTPException(404, "message not found")
        s.add(Feedback(org_id=org.id, message_id=msg.id, value=body.value,
                       comment=body.comment, sensitive=body.sensitive))
        context_id = msg.context_id
    # Link feedback to the context graph node (best-effort).
    if context_id:
        try:
            await ContextGraph(context_id, str(org.id)).add_feedback(
                value=body.value, comment=body.comment, sensitive=body.sensitive)
        except Exception as e:  # noqa: BLE001
            log.warning("feedback.cg_failed", error=str(e))
    return {"status": "recorded"}
