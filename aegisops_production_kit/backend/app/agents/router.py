"""Router agent — explainable intent classification + ServiceNow ticket creation.

Classifies into cloudops/devops/sre/knowledge/general with confidence + reason. For CloudOps
it also extracts cloud + resource + action so the agent can select the right multi-cloud
template. Creates a real ServiceNow SR/CR/Incident for actionable intents and opens the
context graph. Low confidence / ambiguous → ask the user to clarify (no destructive action).
"""

from __future__ import annotations

import json

import structlog

from ..graph_db.context_graph import ContextGraph
from ..integrations.gemini import get_gemini
from ..integrations.servicenow import get_servicenow
from ..metrics import AGENT_RUNS
from ..settings import get_settings
from . import intent_guard, llm, memory, params, templates
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

_SYSTEM = """You are the Router for AegisOps, an agentic AIOps platform.
Classify the user's request and respond with ONLY a JSON object, no prose.

Domains:
- cloudops: provision/modify/destroy cloud infrastructure (AWS/Azure/GCP/Kubernetes/VMware) — S3, EC2, RDS, VPC, EKS, storage, buckets, any cloud resource.
- devops: source/CI/CD/deployments (GitHub repos, Actions, build images, deploy to K8s).
- sre: incidents, reliability, triage, RCA, remediation, latency/error investigations.
- knowledge: questions answered from runbooks/RCAs/design docs (no side effects).
- general: everything else / conversational.

For cloudops, also identify cloud (aws|azure|gcp|kubernetes|vmware), resource (s3|ec2|rds|vpc|eks|vm|aks|postgres|gke|cloudsql|storage|resource_group|gcs|database|module|other), and action:
- create: provision a brand-new resource. Requires an explicit instruction (create/provision/launch/deploy…).
- read: query existing resources or account state — counts, listings, status, attributes. ANY question
  ("how many…", "are any… running", "what is…", "did I create…", "is it created…", "list…") is ALWAYS
  action=read — a question is never create/modify/destroy.
- modify: change an EXISTING resource (e.g. add security-group inbound ports to an instance).
- destroy: tear down an existing resource. ONLY when the user explicitly asks to destroy/delete/remove/terminate.
Also identify `target`: the name or reference of the EXISTING resource a read/modify/destroy acts on
(e.g. "test-vm", "the instance I created", "the earlier EC2"), or null for a brand-new create.
For a broad question about everything created ("did I create any resources", "list my resources"),
use action "read" and target "all".

Available CloudOps templates:
{catalog}

JSON shape:
{{"domain": "...", "cloud": "aws|azure|gcp|null", "resource": "...|null", "action": "create|modify|destroy|read|null",
  "target": "<existing resource name/reference>|null",
  "intent": "<short label>", "confidence": 0.0-1.0, "reason": "<one sentence>"}}"""


async def router(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    message = state["message"]
    await emitter.step(0, "Understood intent")

    # If we're mid parameter-collection for this session AND this message plausibly answers the
    # pending request, continue the SAME provisioning request (reuse cloud/resource/ticket, no
    # re-classification, no new ServiceNow ticket). A question or a new request must NEVER be
    # swallowed by the pending record (BUG-01 root cause: a stale destroy_vpc collection hijacked
    # "How many s3 buckets are running in aws?" and re-asked for a VPC name — screenshots 19/20).
    session_id = state.get("session_id")
    if session_id:
        pending = await params.load_pending(session_id)
        if pending:
            if intent_guard.is_new_request(message):
                # Topic change: abandon the pending collection and classify this message fresh.
                await params.clear_pending(session_id)
                log.info("router.pending_abandoned", session_id=session_id,
                         pending_intent=pending.get("intent"), pending_action=pending.get("action"),
                         shape=intent_guard.message_shape(message))
                await emitter.step(1, f"Set aside the pending {pending.get('intent', 'request')} — "
                                      "handling your new message")
            else:
                await emitter.step(1, f"Continuing → {pending.get('cloud', 'cloud')} provisioning")
                return {"domain": "cloudops", "collecting": True,
                        "intent": pending.get("intent", "provision"), "intent_confidence": 1.0,
                        "routing_reason": "Collecting the required parameters for your pending request.",
                        "cloud": pending.get("cloud"), "resource": pending.get("resource"),
                        "action": pending.get("action", "create"),
                        "snow_id": pending.get("snow_id"), "context_id": pending.get("context_id")}

    gemini = get_gemini(settings)
    if not gemini.enabled:
        log.warning("router.llm_unavailable")
        return {"domain": "general", "llm_unavailable": True, "intent": "unavailable",
                "intent_confidence": 0.0, "routing_reason": "LLM not configured"}

    system = _SYSTEM.format(catalog=json.dumps(templates.catalog(), indent=0))
    # Session memory (Phase 8 / N-03): recent turns let the classifier resolve references
    # ("do that again", "the previous one", "same but in gcp") against what was actually said.
    # M3: the router gets a purpose-shaped context slice (summary + recent + retrieval), not just
    # the last 8 turns — so a reference to something said 30 turns ago still resolves.
    ctx = await memory.build_context(session_id or "", purpose="router", current_message=message,
                                     settings=get_settings(), org_id=state.get("org_id"),
                                     user_id=state.get("user", {}).get("user_id"))
    classify_input = (f"Recent conversation (context for resolving references — classify ONLY "
                      f"the current message):\n{ctx}\n\nCurrent message: {message}") if ctx else message
    try:
        cls = await llm.classify_json(settings, system, classify_input)
    except Exception as e:  # noqa: BLE001
        log.warning("router.classify_failed", error=str(e))
        return {"domain": "general", "intent": "general", "intent_confidence": 0.3,
                "routing_reason": f"classification fallback ({e})"}

    domain = (cls.get("domain") or "general").lower()
    if domain not in {"cloudops", "devops", "sre", "knowledge", "general"}:
        domain = "general"
    confidence = float(cls.get("confidence", 0.5))
    reason = cls.get("reason", "")
    intent = cls.get("intent", domain)

    await emitter.step(1, f"Routed → {domain} ({int(confidence * 100)}%)")
    AGENT_RUNS.labels(domain=domain, workflow=intent, status="routed", env=state.get("user", {}).get("env", "na")).inc()

    updates: dict = {
        "domain": domain,
        "intent": intent,
        "intent_confidence": confidence,
        "routing_reason": reason,
        "cloud": (cls.get("cloud") or "").lower() or None,
        "resource": (cls.get("resource") or "").lower() or None,
        "action": (cls.get("action") or "create").lower(),
        "target": (cls.get("target") or "").strip() or None,
    }

    # Hard safety guard (Phase 7 / BUG-01): deterministic, regex-only — even if the LLM misfires,
    # a status/inventory question can never carry a side-effecting action, and a destroy requires
    # the user's own explicit destructive verb. This is what makes "How many s3 buckets are
    # running?" → destroy_vpc (screenshot 20) structurally impossible.
    guarded = intent_guard.guard_classification(message, updates)
    if guarded:
        note = guarded.pop("guard_note", "read-only downgrade")
        log.warning("router.safety_guard", note=note, original_intent=intent,
                    original_action=updates.get("action"), confidence=confidence)
        updates.update(guarded)
        intent = updates.get("intent", intent)
        await emitter.step(1, f"Safety guard · {note}")
        if updates.get("needs_clarification"):
            return updates

    # Broad inventory question with no usable target → list everything (Phase 7 / BUG-04).
    if (updates["domain"] == "cloudops" and updates["action"] == "read"
            and not updates.get("target") and intent_guard.is_broad_inventory_question(message)):
        updates["target"] = "all"

    # Ambiguity guard — never take destructive action on unclear intent.
    if confidence < 0.45:
        updates["needs_clarification"] = True
        updates["clarification"] = (
            "I want to make sure I route this correctly — could you clarify the goal "
            "(e.g. provision a resource, investigate an incident, deploy code, or ask a question)?"
        )
        return updates

    # Create a real ServiceNow ticket for actionable domains + open the context graph.
    actionable = domain in {"cloudops", "devops", "sre"}
    context_id = state.get("context_id") or state["run_id"]
    if actionable:
        await _create_ticket(state, domain, intent, message, updates)
    try:
        cg = ContextGraph(context_id, state.get("org_id", ""))
        await cg.create(trigger=message, snow_id=updates.get("snow_id"),
                        env=state.get("user", {}).get("env"), trace_id=state.get("trace_id"))
        await cg.set_intent(intent=intent, confidence=confidence, reason=reason, domain=domain)
    except Exception as e:  # noqa: BLE001 - context graph is best-effort, never blocks the run
        log.warning("router.context_graph_failed", error=str(e))
    return updates


async def _create_ticket(state: AgentState, domain: str, intent: str, message: str, updates: dict) -> None:
    snow = get_servicenow(get_settings())
    if not snow.enabled:
        return
    try:
        short = f"AegisOps: {intent}"
        if domain == "sre":
            res = await snow.create_incident(short, message, urgency="2")
            updates["snow_table"] = "incident"
        elif domain == "cloudops" and state.get("user", {}).get("env") == "Production":
            res = await snow.create_change_request(short, message, risk="moderate")
            updates["snow_table"] = "change_request"
        else:
            res = await snow.create_service_request(short, message)
            updates["snow_table"] = "sc_request"
        updates["snow_id"] = res.get("number")
        updates["snow_sys_id"] = res.get("sys_id")
        log.info("router.ticket_created", table=updates["snow_table"], number=res.get("number"))
    except Exception as e:  # noqa: BLE001
        log.warning("router.ticket_failed", error=str(e))


def route_decision(state: AgentState) -> str:
    """Conditional edge: pick the next node from the router's classification."""
    if state.get("needs_clarification"):
        return "general"
    return state.get("domain", "general")
