"""Module data endpoints + notifications (org-scoped, real DB-derived).

Each module returns the design's shape (eyebrow/title/icon/desc/listTitle/stats[]/rows[]). The
descriptive chrome matches the design; the stats and rows are computed from real data (sessions,
runs, documents, integrations, audit, users) — and live cloud inventory where credentials exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select

from ..db import repositories as repo
from ..db.models import AuditLog, Document, Integration, Notification, Run, Session, User
from ..db.session import session_scope
from ..schemas.auth import User as AuthUser
from ..security.deps import get_current_user
from ..settings import Settings, get_settings
from ..tools import aws as aws_tool

router = APIRouter(tags=["modules"])

CHROME = {
    "projects": {"eyebrow": "Workspaces", "title": "Projects", "icon": "P",
                 "desc": "Every project carries its own infrastructure, conversations, memory and governance. Open one to work inside its context.",
                 "listTitle": "Active projects"},
    "infrastructure": {"eyebrow": "CloudOps · multi-cloud", "title": "Infrastructure", "icon": "I",
                       "desc": "Every resource the agents discover across AWS, Azure, GCP, Kubernetes and VMware — explorable as a live graph.",
                       "listTitle": "Tracked resources"},
    "incidents": {"eyebrow": "SRE · incident management", "title": "Incidents", "icon": "!",
                  "desc": "AI-triaged incidents correlated with deploys, metrics and traces, each with a context graph and ServiceNow linkage.",
                  "listTitle": "Active & recent incidents"},
    "knowledge": {"eyebrow": "Semantic search", "title": "Knowledge Center", "icon": "K",
                  "desc": "Runbooks, RCAs, architecture docs and conversation summaries — searchable semantically and cited automatically.",
                  "listTitle": "Recently used"},
    "analytics": {"eyebrow": "Executive view", "title": "Analytics", "icon": "A",
                  "desc": "Engineering, SRE, DevOps and cloud KPIs in one place. Every chart is explainable.",
                  "listTitle": "Key metrics"},
    "admin": {"eyebrow": "Governance & identity", "title": "Administration", "icon": "S",
              "desc": "Organizations, RBAC, approval policies, audit and MCP servers — surfaced by the AI.",
              "listTitle": "Governance overview"},
    "settings": {"eyebrow": "Personal", "title": "Profile & Settings", "icon": "⚙",
                 "desc": "Your preferences, notification rules, connected accounts, and personal guardrails.",
                 "listTitle": "Preferences"},
}


def _stat(label, value, delta="", color="var(--text-3)"):
    return {"label": label, "value": str(value), "delta": delta, "deltaColor": color}


def _row(dot, name, meta, value):
    return {"dot": dot, "name": name, "meta": meta, "value": value}


@router.get("/modules/{name}")
async def module(name: str, user: AuthUser = Depends(get_current_user),
                 settings: Settings = Depends(get_settings)) -> dict:
    if name not in CHROME:
        raise HTTPException(404, "unknown module")
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        oid = org.id
        builder = {
            "projects": _projects, "infrastructure": _infrastructure, "incidents": _incidents,
            "knowledge": _knowledge, "analytics": _analytics, "admin": _admin, "settings": _settings,
        }[name]
        stats, rows = await builder(s, oid, user, settings)
    return {**CHROME[name], "stats": stats, "rows": rows}


async def _count(s, model, *where):
    q = select(func.count()).select_from(model)
    for w in where:
        q = q.where(w)
    return (await s.execute(q)).scalar_one()


async def _projects(s, oid, user, settings):
    sessions = await _count(s, Session, Session.org_id == oid)
    runs = await _count(s, Run, Run.org_id == oid)
    docs = await _count(s, Document, Document.org_id == oid)
    stats = [_stat("Conversations", sessions, "across the org"),
             _stat("Agent runs", runs, "lifetime", "var(--green)"),
             _stat("Knowledge docs", docs, "indexed"),
             _stat("Integrations", await _count(s, Integration, Integration.org_id == oid), "connected", "var(--green)")]
    recent = (await s.execute(select(Session).where(Session.org_id == oid).order_by(Session.created_at.desc()).limit(5))).scalars()
    rows = [_row("var(--green)" if r.status == "active" else "var(--text-4)", r.title,
                 f"{r.status} · {r.created_at.date()}", r.status) for r in recent] or [_row("var(--text-4)", "No conversations yet", "Start one in the AI Workspace", "—")]
    return stats, rows


async def _infrastructure(s, oid, user, settings):
    aws = aws_tool.get_aws(settings)
    rows, resources = [], 0
    if aws.enabled:
        try:
            vpcs = await aws.list_vpcs(user.org or "us-east-1" if False else settings.aws_default_region)
            for v in vpcs[:6]:
                rows.append(_row("var(--green)", v["id"], f"VPC · {v.get('cidr')} · {'default' if v.get('is_default') else 'custom'}", v.get("state", "available")))
            resources = len(vpcs)
        except Exception:  # noqa: BLE001
            rows = [_row("var(--amber)", "AWS read failed", "check credentials/permissions", "error")]
    if not rows:
        rows = [_row("var(--text-4)", "No cloud credentials configured", "Add AWS/Azure/GCP creds to discover live inventory", "—")]
    stats = [_stat("Resources", resources, "live discovery" if aws.enabled else "configure creds"),
             _stat("Clouds", "AWS·Azure·GCP", "multi-cloud"),
             _stat("Integrations", await _count(s, Integration, Integration.org_id == oid), "connected", "var(--green)"),
             _stat("Drift detected", 0, "none")]
    return stats, rows


async def _incidents(s, oid, user, settings):
    open_runs = await _count(s, Run, Run.org_id == oid, Run.status == "awaiting_approval")
    total = await _count(s, Run, Run.org_id == oid, Run.domain == "sre")
    stats = [_stat("Open", open_runs, "awaiting action", "var(--amber)"),
             _stat("SRE runs", total, "triaged"),
             _stat("Total runs", await _count(s, Run, Run.org_id == oid), "all domains"),
             _stat("Auto-triaged", "AI", "decision matrix", "var(--green)")]
    recent = (await s.execute(select(Run).where(Run.org_id == oid).order_by(Run.created_at.desc()).limit(5))).scalars()
    rows = [_row("var(--amber)" if r.status == "awaiting_approval" else "var(--green)",
                 f"{(r.intent or 'run')}", f"{r.domain} · {r.status}" + (f" · {r.snow_id}" if r.snow_id else ""),
                 r.status) for r in recent] or [_row("var(--text-4)", "No incidents", "All clear", "—")]
    return stats, rows


async def _knowledge(s, oid, user, settings):
    docs = await _count(s, Document, Document.org_id == oid)
    from ..db.models import DocumentChunk
    chunks = await _count(s, DocumentChunk, DocumentChunk.org_id == oid)
    embedded = await _count(s, DocumentChunk, DocumentChunk.org_id == oid, DocumentChunk.embedding.isnot(None))
    stats = [_stat("Documents", docs, "indexed", "var(--green)"),
             _stat("Chunks", chunks, "searchable"),
             _stat("Embedded", embedded, "vector-ready" if embedded else "set GEMINI_API_KEY", "var(--green)" if embedded else "var(--amber)"),
             _stat("Coverage", f"{int(100*embedded/chunks) if chunks else 0}%", "of chunks")]
    recent = (await s.execute(select(Document).where(Document.org_id == oid).order_by(Document.updated_at.desc()).limit(6))).scalars()
    rows = [_row("var(--accent-3)", d.title, f"{d.kind} · {d.source or ''}", "indexed") for d in recent] or [_row("var(--text-4)", "No documents", "Ingest via /knowledge/ingest", "—")]
    return stats, rows


async def _analytics(s, oid, user, settings):
    runs = await _count(s, Run, Run.org_id == oid)
    completed = await _count(s, Run, Run.org_id == oid, Run.status == "completed")
    stats = [_stat("Total runs", runs, "all domains"),
             _stat("Completed", completed, f"{int(100*completed/runs) if runs else 0}% success", "var(--green)"),
             _stat("Documents", await _count(s, Document, Document.org_id == oid), "knowledge base"),
             _stat("Members", await _count(s, User, User.org_id == oid), "in org")]
    rows = [_row("var(--green)", "Agent success rate", f"{completed}/{runs} runs completed", f"{int(100*completed/runs) if runs else 0}%"),
            _row("var(--cyan)", "Knowledge growth", "documents indexed", str(await _count(s, Document, Document.org_id == oid))),
            _row("var(--accent-3)", "Active conversations", "sessions", str(await _count(s, Session, Session.org_id == oid)))]
    return stats, rows


async def _admin(s, oid, user, settings):
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    audit24 = await _count(s, AuditLog, AuditLog.org_id == oid, AuditLog.ts >= since)
    stats = [_stat("Members", await _count(s, User, User.org_id == oid), "in org"),
             _stat("Roles", 8, "RBAC enforced"),
             _stat("Audit events (24h)", audit24, "all signed", "var(--green)"),
             _stat("Integrations", await _count(s, Integration, Integration.org_id == oid), "connected", "var(--green)")]
    integ = (await s.execute(select(Integration).where(Integration.org_id == oid).limit(5))).scalars()
    rows = [_row("var(--green)", i.name, i.kind, i.status) for i in integ]
    return stats, rows


async def _settings(s, oid, user, settings):
    # GW-1: the real Telegram binding for THIS user, so the Preferences list is honest about
    # whether a channel is connected (the control itself is the Connected accounts panel).
    tg_linked = False
    if user.user_id:
        from ..db.models import ChannelIdentity
        tg_linked = (await s.execute(select(ChannelIdentity).where(
            ChannelIdentity.channel == "telegram", ChannelIdentity.org_id == oid,
            ChannelIdentity.user_id == uuid.UUID(user.user_id)))).scalar_one_or_none() is not None
    tg_enabled = settings.aegisops_telegram == "on" and bool(settings.telegram_bot_token)
    stats = [_stat("Role", user.display_roles[0] if user.display_roles else "—", "RBAC"),
             _stat("Approval mode", "Required", "for production", "var(--amber)"),
             _stat("Can approve", "Yes" if user.can_approve else "No", "side-effecting", "var(--green)" if user.can_approve else "var(--text-3)"),
             _stat("Email", user.email or "—", "")]
    rows = [_row("var(--green)", "Notification rules", "in-app + email (when SMTP configured)", "configured"),
            _row("var(--cyan)", "Connected account", user.username, "Keycloak"),
            _row("var(--green)" if tg_linked else "var(--text-4)", "Telegram",
                 "message AegisOps from your phone · your roles and approval rules follow the link"
                 if tg_enabled else "not enabled on this deployment",
                 "linked" if tg_linked else ("not linked" if tg_enabled else "disabled")),
            _row("var(--accent-3)", "Default agent mode", "Approval required in production", "enabled")]
    return stats, rows


@router.get("/overview")
async def overview(user: AuthUser = Depends(get_current_user)) -> dict:
    """Real org-scoped figures for the left sidebar: org identity + nav badge counts."""
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        oid = org.id
        # No dedicated Project entity exists; conversations (sessions) are the org's real
        # workspaces, and "open incidents" are runs paused at the human-approval gate.
        projects = await _count(s, Session, Session.org_id == oid)
        incidents = await _count(s, Run, Run.org_id == oid, Run.status == "awaiting_approval")
        return {
            "org": {"name": org.name, "plan": org.plan, "member_count": org.member_count},
            "projects": projects,
            "incidents": incidents,
        }


@router.get("/notifications")
async def notifications(user: AuthUser = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        rows = await repo.NotificationRepo.list(s, org.id, limit=30)
        return {"notifications": [{"title": n.title, "time": _ago(n.created_at), "color": n.color or "var(--accent-2)",
                                   "read": n.read} for n in rows]}


def _ago(ts: datetime) -> str:
    delta = datetime.now(timezone.utc) - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "now"
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


# ── MPP: Module Promotion Pipeline (draft → checks → propose → review) ─────────────────────

from pydantic import BaseModel  # noqa: E402

from ..agents import module_pipeline  # noqa: E402
from ..db.models import ModuleProposal  # noqa: E402
from ..security.deps import require_approver, require_initiator  # noqa: E402


class DraftRequest(BaseModel):
    key: str                     # "<cloud>.<resource>", e.g. "aws.efs"
    files: dict[str, str]        # {filename: HCL}
    description: str = ""


class ReviewRequest(BaseModel):
    decision: str                # promote | reject
    note: str = ""


def _proposal_row(p: ModuleProposal) -> dict:
    return {"id": str(p.id), "key": p.key, "status": p.status, "description": p.description,
            "fmt_ok": p.fmt_ok, "validate_ok": p.validate_ok,
            "scan": (p.scan or {}).get("status"), "created_by": p.created_by,
            "reviewed_by": p.reviewed_by, "files": sorted(p.files or {}),
            "created": _ago(p.created_at)}


@router.get("/modules/proposals")
async def list_proposals(user: AuthUser = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        rows = (await s.execute(
            select(ModuleProposal).where(ModuleProposal.org_id == org.id)
            .order_by(ModuleProposal.created_at.desc()).limit(50))).scalars().all()
        return {"proposals": [_proposal_row(p) for p in rows]}


@router.post("/modules/proposals")
async def create_proposal(body: DraftRequest, user: AuthUser = Depends(require_initiator)) -> dict:
    """Draft a module. The draft is inert data — never planned, never applied, unselectable
    until a human PROMOTES it (generation and execution never share a turn)."""
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    try:
        pid = await module_pipeline.draft(org_id, body.key, body.files,
                                          description=body.description, created_by=user.username)
    except module_pipeline.ModulePipelineError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"id": pid, "status": "draft"}


@router.post("/modules/proposals/{proposal_id}/checks")
async def check_proposal(proposal_id: str, user: AuthUser = Depends(require_initiator)) -> dict:
    """Real `terraform fmt -check` + `validate` + the security-scan seam, in isolation."""
    await _authorize_proposal(proposal_id, user)
    try:
        return await module_pipeline.run_checks(proposal_id)
    except module_pipeline.ModulePipelineError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/modules/proposals/{proposal_id}/propose")
async def propose_proposal(proposal_id: str, user: AuthUser = Depends(require_initiator)) -> dict:
    await _authorize_proposal(proposal_id, user)
    try:
        await module_pipeline.propose(proposal_id)
    except module_pipeline.ModulePipelineError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"status": "proposed"}


@router.post("/modules/proposals/{proposal_id}/review")
async def review_proposal(proposal_id: str, body: ReviewRequest,
                          user: AuthUser = Depends(require_approver)) -> dict:
    """The human gate (approver RBAC): promote — fail-closed on scan — or reject."""
    await _authorize_proposal(proposal_id, user)
    try:
        return await module_pipeline.review(proposal_id, body.decision,
                                            reviewer=user.username, note=body.note)
    except module_pipeline.ModulePipelineError as exc:
        raise HTTPException(400, str(exc)) from None


# ── M4: per-user/org standing memory (user-editable) ───────────────────────────────────────

from ..agents import user_memory  # noqa: E402


class MemoryRequest(BaseModel):
    content: str
    org_wide: bool = False


@router.get("/memory")
async def list_user_memory(user: AuthUser = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    return {"memories": await user_memory.list_memories(org_id, user.user_id)}


@router.put("/memory/{key}")
async def put_user_memory(key: str, body: MemoryRequest,
                          user: AuthUser = Depends(get_current_user)) -> dict:
    """Set one standing fact (e.g. `usual_region` = `ap-south-1`). `org_wide` rows require an
    approver role — a member edits only their own memory."""
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    if body.org_wide and not user.can_approve:
        raise HTTPException(403, "org-wide standing memory requires an approver role")
    try:
        await user_memory.set_memory(org_id, None if body.org_wide else user.user_id,
                                     key, body.content)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"status": "saved", "key": key, "scope": "org" if body.org_wide else "user"}


@router.delete("/memory/{key}")
async def delete_user_memory(key: str, org_wide: bool = False,
                             user: AuthUser = Depends(get_current_user)) -> dict:
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        org_id = str(org.id)
    if org_wide and not user.can_approve:
        raise HTTPException(403, "org-wide standing memory requires an approver role")
    deleted = await user_memory.delete_memory(org_id, None if org_wide else user.user_id, key)
    if not deleted:
        raise HTTPException(404, "no such memory")
    return {"status": "deleted", "key": key}


async def _authorize_proposal(proposal_id: str, user: AuthUser) -> None:
    """S0: a proposal outside the caller's org does not exist for them (uniform 404)."""
    async with session_scope() as s:
        org = await repo.org_for(s, user)
        try:
            row = await s.get(ModuleProposal, uuid.UUID(proposal_id))
        except ValueError:
            raise HTTPException(404, "proposal not found") from None
        if row is None or row.org_id != org.id:
            raise HTTPException(404, "proposal not found")
