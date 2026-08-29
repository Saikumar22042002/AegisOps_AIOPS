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
from ..integrations.gemini import GeminiError
from ..rag import retriever
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools.kubernetes import KubernetesError, get_kubernetes
from ..tools.prometheus import get_prometheus
from . import memory
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


async def _collect_telemetry(settings, emitter, *, run_id: str | None = None,
                             org_id: str | None = None, context: str | None = None) -> dict:
    prom = get_prometheus(settings)
    # U2: recent_deploy is a REAL Prometheus signal (a deployment generation change in the last
    # 15m via kube-state-metrics), not the old hardcoded True. Default False when Prometheus is
    # unavailable or the metric is absent — we never assume a deploy happened.
    signals: dict = {"recent_deploy": False}
    try:
        if prom.enabled and await prom.ping():
            up = await prom.scalar("sum(up)", default=0)
            await emitter.console("stdout", f"prometheus: targets up = {int(up)}")
            signals["targets_up"] = up
            signals["error_rate"] = await prom.scalar(
                "sum(rate(aegisops_api_requests_total{status=~\"5..\"}[5m])) / "
                "clamp_min(sum(rate(aegisops_api_requests_total[5m])),1)", default=0.0)
            deploy_changes = await prom.scalar(
                "sum(changes(kube_deployment_status_observed_generation[15m]))", default=0.0)
            signals["recent_deploy"] = deploy_changes > 0
            signals["cpu_saturation"] = await prom.scalar(
                "max(1 - rate(node_cpu_seconds_total{mode=\"idle\"}[5m]))", default=0.0)
            signals["pod_restarts"] = await prom.scalar(
                "sum(increase(kube_pod_container_status_restarts_total[15m]))", default=0.0)
            await emitter.console("stdout", f"prometheus: recent_deploy={signals['recent_deploy']} "
                                            f"error_rate={signals['error_rate']:.3f}")
    except Exception as e:  # noqa: BLE001
        await emitter.console("stderr", f"prometheus query failed: {e}")

    # INV: K8s triage evidence through the read-only investigation boundary — the investigator
    # can only reach the frozen read-only registry, so triage work is structurally unable to
    # mutate anything (mutation stays behind the approval gate in sre_execute).
    #
    # P2.2: when the harness-read-paths flag is on, the kernel's OBSERVE→REASON→ACT loop
    # drives that same frozen registry — the deterministic single call becomes a genuine
    # bounded investigation. Flag off ⇒ the exact prior behavior (coexistence, T-P2-01).
    try:
        from ..tools.kubernetes import get_kubernetes as _get_k8s
        if _get_k8s(settings).enabled:
            if getattr(settings, "aegisops_harness_read_paths", "off") == "on":
                from ..harness import inv as harness_inv
                res = await harness_inv.investigate(
                    settings,
                    "Collect Kubernetes triage evidence (deployments, pods, recent restarts) "
                    "relevant to the current incident and summarize what the telemetry shows.",
                    run_id=run_id, org_id=org_id, context=context)
                signals["harness_investigation"] = {
                    "status": res.status, "iterations": res.iterations,
                    "evidence_ok": res.evidence_ok, "findings": res.findings[:600]}
                await emitter.console(
                    "stdout", f"harness INV (read-only): {res.status} in "
                              f"{res.iterations} iteration(s), evidence={res.evidence_ok}")
            else:
                from . import investigation
                inv = investigation.Investigator(investigation.default_registry(settings))
                ev = await inv.call("list_deployments", namespace="default")
                if ev.ok:
                    signals["deployments"] = [d.get("name") for d in (ev.result or [])][:10]
                    await emitter.console(
                        "stdout", f"investigation (read-only): {len(ev.result or [])} deployments")
                else:
                    await emitter.console("stderr", f"investigation: {ev.error}")
    except Exception as e:  # noqa: BLE001 — triage evidence is best-effort
        await emitter.console("stderr", f"investigation failed: {e}")
    return signals


async def sre_analyze(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    await emitter.step(2, "Triaging incident")

    # Prompt 3 (mandate 24): the investigator reasons over the TYPED intelligence slice —
    # incident history, recent changes, past sessions — not a bare objective.
    sre_context = await memory.build_context(state.get("session_id", ""), purpose="sre",
                                             org_id=state.get("org_id"),
                                             user_id=state.get("user", {}).get("user_id"),
                                             current_message=state.get("message"),
                                             settings=settings, run_id=state.get("run_id"))
    signals = await _collect_telemetry(settings, emitter, run_id=state.get("run_id"),
                                       org_id=state.get("org_id"), context=sre_context)

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
        analysis = await llm.stream_answer(settings, _SYSTEM, prompt, emitter,
                                           purpose="sre.triage")
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
    action = decision.get("action")
    namespace = decision.get("namespace", "default")

    # U2: execute the REAL K8s remediation when a cluster is configured; otherwise report
    # "proposed, not executed" honestly (never a fake applied:True). All of this is already
    # behind the approval gate — this node only runs after the human approved.
    if not k8s.enabled:
        note = (f"Remediation **proposed, not executed**: `{action}` on `{target}`. No Kubernetes "
                "cluster is configured (set KUBECONFIG) — this is a recommendation for a human.")
        await emitter.console("stdout", f"no kubeconfig · '{action}' on {target} proposed, not executed")
        await emitter.token(note)
        await cg.update_step(order=3, status="done", result={"applied": False, "proposed": action, "target": target})
        return {"outcome": {"status": "proposed_not_executed", "applied": False, "decision": decision},
                "answer": note, "tool_results": [{"proposed_remediation": decision, "applied": False}]}

    if action == "investigate":
        note = f"No automated remediation matched — human investigation recommended for `{target}`."
        await emitter.token(note)
        await cg.update_step(order=3, status="done", result={"applied": False, "proposed": action})
        return {"outcome": {"status": "proposed_not_executed", "applied": False, "decision": decision},
                "answer": note, "tool_results": [{"proposed_remediation": decision, "applied": False}]}

    try:
        if action == "restart":
            result = await k8s.restart_deployment(target, namespace)
        elif action == "scale_out":
            current = next((d for d in await k8s.list_deployments(namespace) if d["name"] == target), None)
            replicas = (current.get("replicas") or 1) + 1 if current else 2
            result = await k8s.scale_deployment(target, replicas, namespace)
        elif action == "rollback":
            result = await k8s.rollback_deployment(target, namespace)
        else:
            raise KubernetesError(f"unsupported remediation action '{action}'")
        msg = f"✅ Remediation applied: **{action}** on `{target}` — {result}"
        await emitter.console("stdout", f"k8s: {action} on {target} applied · {result}")
        await emitter.token(msg)
        await cg.update_step(order=3, status="done", result={"applied": True, **result})
        return {"outcome": {"status": "remediated", "applied": True, "decision": decision, "result": result},
                "answer": msg, "tool_results": [{"remediation": decision, "applied": True, "result": result}]}
    except Exception as e:  # noqa: BLE001 — a failed real remediation is reported truthfully
        await emitter.error(f"remediation '{action}' failed: {e}", code="remediation_error", retriable=True)
        await cg.update_step(order=3, status="failed", error=str(e))
        return {"outcome": {"status": "remediation_failed", "applied": False, "error": str(e)[:400],
                            "decision": decision}}
