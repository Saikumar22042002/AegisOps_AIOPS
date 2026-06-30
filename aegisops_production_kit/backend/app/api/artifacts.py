"""Per-run artifact endpoints — real run data for the 8 artifact tabs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..db.models import Approval, Message, Run
from ..db.session import session_scope
from ..schemas.auth import User
from ..security.deps import get_current_user
from ..settings import Settings, get_settings
from ..tools.prometheus import get_prometheus

router = APIRouter(tags=["artifacts"])


async def _load(run_id: str):
    async with session_scope() as s:
        run = await s.get(Run, uuid.UUID(run_id))
        if not run:
            raise HTTPException(404, "run not found")
        msg = (await s.execute(
            select(Message).where(Message.run_id == run.id, Message.role == "assistant")
            .order_by(Message.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        approvals = list((await s.execute(
            select(Approval).where(Approval.run_id == run.id).order_by(Approval.ts)
        )).scalars())
        return run, msg, approvals


def _node(title, detail, time, status, last=False):
    return {"title": title, "detail": detail, "time": time, "status": status, "last": last}


@router.get("/runs/{run_id}/timeline")
async def timeline(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals = await _load(run_id)
    approved = run.status in {"completed"} and any(a.decision == "approved" for a in approvals)
    rejected = any(a.decision == "rejected" for a in approvals)
    has_plan = bool(run.plan_json)
    nodes = [
        _node("Router", f"Classified → {run.intent or 'intent'}", "0.3s", "done"),
        _node(f"{(run.domain or 'agent').title()} Agent", run.routing_reason or "Processed request", "—", "done" if run.domain else "queued"),
    ]
    if has_plan:
        summ = run.plan_json.get("summary", {})
        nodes.append(_node("Policy Evaluation", f"{sum(1 for p in run.plan_json.get('policy_checks', []) if p.get('passed'))} checks passed", "0.4s", "done"))
        nodes.append(_node("Planner", f"+{summ.get('add',0)} ~{summ.get('change',0)} -{summ.get('destroy',0)}", "—", "done"))
        nodes.append(_node("Human Approval",
                           "Rejected — halted" if rejected else ("Approved" if approved else "Awaiting your decision"),
                           "—" if (approved or rejected) else "···",
                           "rejected" if rejected else ("done" if approved else "pending")))
        exec_status = "done" if (run.outcome or {}).get("status") in {"applied", "destroyed"} else ("cancelled" if rejected else "queued")
        nodes.append(_node("Execute", (run.outcome or {}).get("status", "queued"), "—", exec_status))
    nodes.append(_node("Finalize", run.outcome.get("resolution", run.status) if run.outcome else run.status, "—",
                       "done" if run.status == "completed" else "queued", last=True))
    elapsed = "running" if run.status == "running" else ("halted" if rejected else "paused" if run.status == "awaiting_approval" else "completed")
    return {"nodes": nodes, "elapsed": elapsed, "mode": run.mode}


@router.get("/runs/{run_id}/reasoning")
async def reasoning(run_id: str, user: User = Depends(get_current_user)) -> dict:
    _run, msg, _ = await _load(run_id)
    cards = (msg.analysis or {}).get("reasoning", []) if msg else []
    return {"cards": cards}


@router.get("/runs/{run_id}/terraform")
async def terraform(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, _ = await _load(run_id)
    plan = run.plan_json or {}
    return {"summary": plan.get("summary", {"add": 0, "change": 0, "destroy": 0}),
            "diff": plan.get("diff", []), "policy_checks": plan.get("policy_checks", []),
            "workspace": plan.get("workspace"), "mode": plan.get("mode", run.mode)}


@router.get("/runs/{run_id}/logs")
async def logs(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals = await _load(run_id)
    lines = [{"ts": run.created_at.strftime("%H:%M:%S"), "lvl": "INFO", "lvlColor": "var(--cyan)",
              "msg": f"intent classified: {run.intent} ({run.confidence})"},
             {"ts": run.created_at.strftime("%H:%M:%S"), "lvl": "INFO", "lvlColor": "var(--cyan)",
              "msg": f"routed -> {run.domain} agent"}]
    if run.plan_json:
        summ = run.plan_json.get("summary", {})
        lines.append({"ts": run.created_at.strftime("%H:%M:%S"), "lvl": "INFO", "lvlColor": "var(--cyan)",
                      "msg": f"plan: +{summ.get('add',0)} ~{summ.get('change',0)} -{summ.get('destroy',0)}"})
        passed = sum(1 for p in run.plan_json.get("policy_checks", []) if p.get("passed"))
        total = len(run.plan_json.get("policy_checks", []))
        lines.append({"ts": run.created_at.strftime("%H:%M:%S"), "lvl": "OK", "lvlColor": "var(--green)",
                      "msg": f"policy: {passed}/{total} passed"})
    for a in approvals:
        lines.append({"ts": a.ts.strftime("%H:%M:%S"), "lvl": "WARN" if a.decision == "rejected" else "OK",
                      "lvlColor": "var(--amber)" if a.decision == "rejected" else "var(--green)",
                      "msg": f"approval {a.decision} by {a.actor_user}"})
    return {"lines": lines}


@router.get("/runs/{run_id}/metrics")
async def metrics(run_id: str, user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> dict:
    prom = get_prometheus(settings)
    cards = []
    try:
        if prom.enabled and await prom.ping():
            up = await prom.scalar("sum(up)")
            rate = await prom.scalar("sum(rate(aegisops_api_requests_total[5m]))")
            cards = [{"label": "Targets up", "value": str(int(up)), "unit": "", "sub": "Prometheus", "subColor": "var(--green)"},
                     {"label": "API req/s", "value": f"{rate:.2f}", "unit": "rps", "sub": "5m rate", "subColor": "var(--text-4)"}]
    except Exception:  # noqa: BLE001
        pass
    if not cards:
        cards = [{"label": "Metrics", "value": "—", "unit": "", "sub": "Prometheus unreachable", "subColor": "var(--amber)"}]
    return {"cards": cards, "source": "prometheus"}


@router.get("/runs/{run_id}/traces")
async def traces(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, _ = await _load(run_id)
    spans = [{"name": "intent.classify", "dur": "—", "dot": "var(--green)", "indent": "0px", "tokens": ""},
             {"name": "agent.route", "dur": "—", "dot": "var(--green)", "indent": "0px", "tokens": ""}]
    if run.plan_json:
        spans.append({"name": "workflow.plan", "dur": "—", "dot": "var(--green)", "indent": "12px", "tokens": ""})
        spans.append({"name": "tool.terraform_plan", "dur": "—", "dot": "var(--green)", "indent": "24px", "tokens": ""})
        spans.append({"name": "approval.gate", "dur": "···", "dot": "var(--amber)", "indent": "0px", "tokens": ""})
    return {"spans": spans, "trace_id": run.trace_id or run_id, "context_id": run.context_id or run_id}


@router.get("/runs/{run_id}/references")
async def references(run_id: str, user: User = Depends(get_current_user)) -> dict:
    _run, msg, _ = await _load(run_id)
    refs = (msg.analysis or {}).get("references", []) if msg else []
    return {"references": refs}


@router.get("/runs/{run_id}/approvals")
async def run_approvals(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals = await _load(run_id)
    return {
        "risk": (run.plan_json or {}).get("risk", "Medium"),
        "cost_impact": (run.plan_json or {}).get("cost", "—"),
        "affected": f"{run.domain} · {run.mode}",
        "servicenow": run.snow_id,
        "policy_checks": (run.plan_json or {}).get("policy_checks", []),
        "decisions": [{"decision": a.decision, "actor": a.actor_user, "role": a.actor_role,
                       "ts": a.ts.isoformat(), "rationale": a.rationale} for a in approvals],
        "status": run.status,
    }
