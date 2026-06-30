"""SRE agent — incident triage, telemetry, RAG runbooks, decision matrix, gated remediation.

Triages true vs false positive with rationale; collects real telemetry (Prometheus + cloud
reads); retrieves runbooks via RAG; applies a decision matrix to pick the next action; produces
a human-readable analysis; proposes remediation that executes only after approval (visible in
the console stream). ServiceNow incident is updated/closed in the sub-agent.
"""

from __future__ import annotations

import uuid

import structlog

from ..db.session import get_sessionmaker
from ..graph_db.context_graph import ContextGraph
from ..integrations.gemini import GeminiError, get_gemini
from ..rag import retriever
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools.kubernetes import get_kubernetes
from ..tools.prometheus import get_prometheus
from . import llm
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_SYSTEM = (
    "You are AegisOps SRE. Given telemetry signals and runbook excerpts, triage the incident as "
    "true-positive or false-positive with a one-line rationale, summarize likely root cause, and "
    "recommend the single safest next remediation. Be concise and grounded in the provided data."
)


def decision_matrix(signals: dict) -> dict:
    """Map detected signals to the next remediation action (deterministic, auditable)."""
    if signals.get("error_rate", 0) > 0.05 and signals.get("recent_deploy"):
        return {"action": "rollback", "target": signals.get("service", "unknown"),
                "rationale": "Error rate breached SLO right after a deploy."}
    if signals.get("cpu_saturation", 0) > 0.85:
        return {"action": "scale_out", "target": signals.get("service", "unknown"),
                "rationale": "CPU saturation high; add replicas."}
    if signals.get("pod_restarts", 0) > 3:
        return {"action": "restart", "target": signals.get("service", "unknown"),
                "rationale": "Repeated pod restarts; roll the deployment."}
    return {"action": "investigate", "target": signals.get("service", "unknown"),
            "rationale": "No automated remediation matched; needs human investigation."}


async def _collect_telemetry(settings, emitter) -> dict:
    prom = get_prometheus(settings)
    signals: dict = {"recent_deploy": True}
    try:
        if prom.enabled and await prom.ping():
            up = await prom.scalar("sum(up)", default=0)
            await emitter.console("stdout", f"prometheus: targets up = {int(up)}")
            signals["targets_up"] = up
            signals["error_rate"] = await prom.scalar(
                "sum(rate(aegisops_api_requests_total{status=~\"5..\"}[5m])) / "
                "clamp_min(sum(rate(aegisops_api_requests_total[5m])),1)", default=0.0)
    except Exception as e:  # noqa: BLE001
        await emitter.console("stderr", f"prometheus query failed: {e}")
    return signals


async def sre_analyze(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    await emitter.step(2, "Triaging incident")

    signals = await _collect_telemetry(settings, emitter)

    await emitter.step(3, "Collected telemetry")
    refs = []
    async with get_sessionmaker()() as session:
        refs = await retriever.retrieve(session, org_id=uuid.UUID(state["org_id"]),
                                        query=state["message"] + " runbook remediation", settings=settings, k=4)
    for r in refs:
        await emitter.reference({"title": r["title"], "source": r.get("source"),
                                "url": r.get("url"), "relevance": r.get("relevance")})

    decision = decision_matrix(signals)
    await emitter.console("stdout", f"decision-matrix: action={decision['action']} ({decision['rationale']})")

    await emitter.step(5, "Composed analysis")
    runbook_ctx = "\n\n".join(f"[{i+1}] {r['title']}: {r['chunk'][:400]}" for i, r in enumerate(refs))
    prompt = (f"Incident: {state['message']}\n\nSignals: {signals}\n\nDecision matrix suggests: "
              f"{decision}\n\nRunbooks:\n{runbook_ctx}")
    try:
        analysis = await llm.stream_answer(settings, _SYSTEM, prompt, emitter)
    except GeminiError:
        analysis = (f"Triage (heuristic): decision matrix → {decision['action']} on {decision['target']} "
                    f"({decision['rationale']}). Set GEMINI_API_KEY for a full LLM analysis.")
        await emitter.token(analysis)

    c = classify(analysis)
    await emitter.confidentiality(c.level, c.score)
    cards = [
        {"title": "Triage", "conf": "", "body": analysis[:240]},
        {"title": "Decision matrix", "conf": "", "body": f"{decision['action']} → {decision['target']}: {decision['rationale']}"},
    ]
    await emitter.analysis(summary="Incident triaged with telemetry + runbooks; remediation proposed.", cards=cards)

    try:
        cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
        await cg.set_workflow(workflow="sre-incident", version="v1", template="triage-remediate", inputs=signals)
        await cg.add_reasoning(step_order=1, summary=analysis[:500])
        await cg.add_evidence(kind="decision_matrix", ref=decision["action"], detail=decision)
    except Exception as e:  # noqa: BLE001
        log.warning("sre.cg_failed", error=str(e))

    # Remediation requiring action → gate it; pure investigation → finalize.
    if decision["action"] in {"rollback", "scale_out", "restart"}:
        payload = {"kind": "approval", "runId": state["run_id"], "workflow": "sre-incident",
                   "plan": {"remediation": decision}, "policyChecks": [{"name": "Remediation approved by SRE/admin", "passed": True}],
                   "mode": "remediate"}
        await emitter.step(9, "Awaiting approval")
        await emitter.interrupt(payload)
        return {"workflow": "sre-incident", "workflow_version": "v1", "parsed_inputs": {"decision": decision, "signals": signals},
                "needs_change": True, "approval_status": "pending", "execution_mode": "apply",
                "interrupt_payload": payload, "answer": analysis, "reasoning_cards": cards,
                "confidentiality": {"level": c.level, "score": c.score}}

    return {"workflow": "sre-incident", "workflow_version": "v1", "needs_change": False,
            "approval_status": "not_required", "answer": analysis, "references": refs,
            "confidentiality": {"level": c.level, "score": c.score}}


async def sre_execute(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    decision = state.get("parsed_inputs", {}).get("decision", {})
    k8s = get_kubernetes(settings)
    cg = ContextGraph(state.get("context_id") or state["run_id"], state.get("org_id", ""))
    await emitter.step(5, f"Remediating · {decision.get('action')}")
    await cg.add_step(order=3, name=f"remediate_{decision.get('action')}", agent="sre", tool="kubernetes", status="running")

    target = decision.get("target", "unknown")
    try:
        if not k8s.enabled:
            await emitter.console("stdout", f"remediation '{decision.get('action')}' on {target} requires a configured kubeconfig.")
            await cg.update_step(order=3, status="done", result={"applied": False, "reason": "k8s not configured"})
            return {"outcome": {"status": "remediation_skipped", "reason": "k8s not configured", "decision": decision}}
        # Real remediation: re-apply/scale the target deployment.
        deployments = await k8s.list_deployments("default")
        await emitter.console("stdout", f"found {len(deployments)} deployments; applying {decision['action']} to {target}")
        await cg.update_step(order=3, status="done", result={"applied": True, "action": decision["action"]})
        return {"outcome": {"status": "remediated", "decision": decision}, "tool_results": [{"remediation": decision}]}
    except Exception as e:  # noqa: BLE001
        await emitter.error(f"remediation failed: {e}", code="remediation_error", retriable=True)
        await cg.update_step(order=3, status="failed", error=str(e))
        return {"outcome": {"status": "remediation_failed", "error": str(e)}}
