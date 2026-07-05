"""Per-run artifact endpoints — real run data for the 8 artifact tabs."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from ..db.models import Approval, Message, Run, RunStep
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
        steps = list((await s.execute(
            select(RunStep).where(RunStep.run_id == run.id).order_by(RunStep.order_index, RunStep.started_at)
        )).scalars())
        return run, msg, approvals, steps


def _node(title, detail, time, status, last=False):
    return {"title": title, "detail": detail, "time": time, "status": status, "last": last}


def _fmt_dur(sec: float) -> str | None:
    """Human duration for a real per-step elapsed time (e.g. 480ms, 3.2s, 1m04s)."""
    if sec is None or sec < 0:
        return None
    if sec < 1:
        return f"{sec * 1000:.0f}ms"
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(round(sec)), 60)
    return f"{m}m{s:02d}s"


def _step_maps(steps: list):
    """(name -> duration string, name -> started 'HH:MM:SS', total run duration) from run_steps."""
    dur: dict[str, str] = {}
    when: dict[str, str] = {}
    for st in steps:
        if st.started_at:
            when[st.name] = st.started_at.strftime("%H:%M:%S")
        if st.started_at and st.ended_at:
            d = _fmt_dur((st.ended_at - st.started_at).total_seconds())
            if d:
                dur[st.name] = d
    starts = [st.started_at for st in steps if st.started_at]
    ends = [st.ended_at for st in steps if st.ended_at]
    total = _fmt_dur((max(ends) - min(starts)).total_seconds()) if starts and ends else None
    return dur, when, total


@router.get("/runs/{run_id}/timeline")
async def timeline(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals, steps = await _load(run_id)
    dur, _when, total = _step_maps(steps)
    approved = run.status in {"completed"} and any(a.decision == "approved" for a in approvals)
    rejected = any(a.decision == "rejected" for a in approvals)
    out_status = (run.outcome or {}).get("status")
    failed = (bool(out_status) and str(out_status).endswith("_failed")) or out_status == "failed" \
        or run.status == "failed"
    has_plan = bool(run.plan_json)
    # Displayed node -> the run_step that timed it (real durations; "—" for legacy runs w/o timings).
    domain_step = {"cloudops": "cloudops_agent", "devops": "devops_plan", "sre": "sre_analyze",
                   "knowledge": "knowledge", "general": "general"}.get(run.domain or "")
    # Real node labels/details — never the "Agent Agent / Processed request" placeholder
    # (Phase 7 / BUG-06). A run persisted without domain/intent is a run that failed before
    # or during classification; say that, plainly.
    run_error = (run.outcome or {}).get("error")
    router_detail = (f"Classified → {run.intent}" if run.intent
                     else ("Run failed before classification completed" if failed else "Classifying…"))
    if run.domain:
        agent_title = f"{run.domain.title()} Agent"
        agent_detail = run.routing_reason or f"Handled by the {run.domain} agent"
        agent_status = "failed" if (failed and not has_plan) else "done"
    else:
        agent_title = "Agent"
        agent_detail = (f"Run failed: {str(run_error)[:160]}" if run_error
                        else ("Run failed before an agent was selected" if failed else "Waiting for routing…"))
        agent_status = "failed" if failed else "queued"
    nodes = [
        _node("Router", router_detail, dur.get("router", "—"), "failed" if (failed and not run.intent) else "done"),
        _node(agent_title, agent_detail, dur.get(domain_step, "—") if domain_step else "—", agent_status),
    ]
    if has_plan:
        summ = run.plan_json.get("summary", {})
        checks = run.plan_json.get("policy_checks", [])
        nodes.append(_node("Policy Evaluation", f"{sum(1 for p in checks if p.get('passed'))}/{len(checks)} checks passed",
                           dur.get("policy_evaluation", "—"), "done"))
        nodes.append(_node("Planner", f"+{summ.get('add',0)} ~{summ.get('change',0)} -{summ.get('destroy',0)}",
                           dur.get("planner", "—"), "done"))
        awaiting = run.status == "awaiting_approval"
        nodes.append(_node("Human Approval",
                           "Rejected — halted" if rejected else ("Approved" if approved else "Awaiting your decision"),
                           dur.get("approval") or ("···" if awaiting else "—"),
                           "rejected" if rejected else ("done" if approved else "pending")))
        applied = out_status in {"applied", "destroyed"}
        exec_status = "done" if applied else ("rejected" if (rejected or failed) else "queued")
        nodes.append(_node("Execute", out_status or "queued", dur.get("execute", "—"), exec_status))
        if applied:
            nodes.append(_node("Verification", "Post-apply read-only checks", dur.get("verify", "—"), "done"))
    nodes.append(_node("Finalize", run.outcome.get("resolution", run.status) if run.outcome else run.status,
                       dur.get("finalize", "—"),
                       "failed" if run.status == "failed" else ("done" if run.status == "completed" else "queued"),
                       last=True))
    elapsed = ("running" if run.status == "running" else "halted" if rejected else "failed" if failed
               else "paused" if run.status == "awaiting_approval" else "completed")
    return {"nodes": nodes, "elapsed": elapsed, "mode": run.mode, "total": total}


@router.get("/runs/{run_id}/reasoning")
async def reasoning(run_id: str, user: User = Depends(get_current_user)) -> dict:
    _run, msg, _, _ = await _load(run_id)
    cards = (msg.analysis or {}).get("reasoning", []) if msg else []
    return {"cards": cards}


@router.get("/runs/{run_id}/terraform")
async def terraform(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, _, _ = await _load(run_id)
    plan = run.plan_json or {}
    return {"summary": plan.get("summary", {"add": 0, "change": 0, "destroy": 0}),
            "diff": plan.get("diff", []), "policy_checks": plan.get("policy_checks", []),
            "workspace": plan.get("workspace"), "mode": plan.get("mode", run.mode)}


@router.get("/runs/{run_id}/logs")
async def logs(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals, _ = await _load(run_id)
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
    run, _msg, _, _ = await _load(run_id)
    spans = [{"name": "intent.classify", "dur": "—", "dot": "var(--green)", "indent": "0px", "tokens": ""},
             {"name": "agent.route", "dur": "—", "dot": "var(--green)", "indent": "0px", "tokens": ""}]
    if run.plan_json:
        spans.append({"name": "workflow.plan", "dur": "—", "dot": "var(--green)", "indent": "12px", "tokens": ""})
        spans.append({"name": "tool.terraform_plan", "dur": "—", "dot": "var(--green)", "indent": "24px", "tokens": ""})
        spans.append({"name": "approval.gate", "dur": "···", "dot": "var(--amber)", "indent": "0px", "tokens": ""})
    return {"spans": spans, "trace_id": run.trace_id or run_id, "context_id": run.context_id or run_id}


@router.get("/runs/{run_id}/references")
async def references(run_id: str, user: User = Depends(get_current_user)) -> dict:
    _run, msg, _, _ = await _load(run_id)
    refs = (msg.analysis or {}).get("references", []) if msg else []
    return {"references": refs}


@router.get("/runs/{run_id}/approvals")
async def run_approvals(run_id: str, user: User = Depends(get_current_user)) -> dict:
    run, _msg, approvals, _ = await _load(run_id)
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
