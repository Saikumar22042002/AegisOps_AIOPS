"""CloudOps agent — multi-cloud provisioning/modify/destroy/read across AWS, Azure, GCP.

Flow: select template (by router's cloud+resource) → extract + Pydantic-validate inputs →
real availability checks (cloud SDK reads) → terraform init/validate/plan → policy + confidentiality
→ build the approval artifact (plan/diff/policy) and INTERRUPT. On approve: terraform apply/destroy
(streamed to the console) → verify via SDK reads → outcome. Cloud SDKs never provision — Terraform
does every mutation, and only after the human-approval gate.
"""

from __future__ import annotations

import json
import re
import uuid

import structlog

from ..db.models import Run
from ..db.session import session_scope
from ..graph_db.context_graph import ContextGraph
from ..integrations.gemini import get_gemini
from ..security import idempotency
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools import aws as aws_tool
from ..tools import azure as azure_tool
from ..tools import gcp as gcp_tool
from ..tools.terraform import TerraformError, TerraformRunner, state_slug
from . import dependency, intent_guard, inventory, llm, params, plan_guard, provider_errors, templates, timing
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


async def _extract_ports(settings, message: str) -> list[int]:
    """Pull the TCP ports the user wants to open (day-2 SG modify)."""
    import re

    ports: list[int] = []
    gemini = get_gemini(settings)
    if gemini.enabled:
        try:
            r = await llm.classify_json(
                settings,
                'Extract only the TCP port numbers the user explicitly wants to open. '
                'Respond with ONLY JSON: {"ingress_ports": [<int>, ...]}.', message)
            ports = [int(p) for p in (r.get("ingress_ports") or []) if 0 < int(p) <= 65535]
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.port_extract_failed", error=str(e))
    if not ports:  # fallback: digits following the word "port(s)"
        m = re.search(r"ports?\b([\d,\s/and]+)", message, re.IGNORECASE)
        if m:
            ports = [int(n) for n in re.findall(r"\d{1,5}", m.group(1)) if 0 < int(n) <= 65535]
    return sorted(set(ports))


async def _extract_modification(settings, message: str) -> dict:
    """MOD: pull the day-2 changes the user asked for. LLM-first with honest regex
    fallbacks; only EXPLICITLY requested changes appear in the result."""
    import re

    changes: dict = {}
    gemini = get_gemini(settings)
    if gemini.enabled:
        try:
            r = await llm.classify_json(
                settings,
                'Extract ONLY the modifications the user explicitly asked for on an existing '
                'cloud resource. Respond with ONLY JSON, omitting keys the user did not ask '
                'about: {"ingress_ports": [<int>], "power": "running|stopped", '
                '"versioning": <bool>, "lifecycle_expire_days": <int>, '
                '"instance_class": "<db.x.y>", "allocated_storage": <int GiB>, '
                '"tags": {"<key>": "<value>"}}. '
                '"start/power on" → power=running; "stop/power off/shut down" → power=stopped.',
                message)
            for k in ("ingress_ports", "power", "versioning", "lifecycle_expire_days",
                      "instance_class", "allocated_storage", "tags"):
                if r.get(k) not in (None, [], {}, ""):
                    changes[k] = r[k]
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.modify_extract_failed", error=str(e))

    low = message.lower()
    if "ingress_ports" not in changes:
        ports = await _extract_ports(settings, message)
        if ports:
            changes["ingress_ports"] = ports
    if "power" not in changes:
        if re.search(r"\b(start|power\s*on|boot|turn\s*on)\b", low):
            changes["power"] = "running"
        elif re.search(r"\b(stop|power\s*off|shut\s*down|turn\s*off)\b", low):
            changes["power"] = "stopped"
    if "versioning" not in changes:
        m = re.search(r"versioning\s*(on|off|enabled?|disabled?|suspend\w*)", low)
        if m:
            changes["versioning"] = m.group(1).startswith(("on", "enable"))
    if "lifecycle_expire_days" not in changes:
        m = re.search(r"(?:expire|lifecycle|delete\s+objects?)\D{0,24}?(\d{1,4})\s*days?", low)
        if m:
            changes["lifecycle_expire_days"] = int(m.group(1))
    if "instance_class" not in changes:
        m = re.search(r"\b(db\.[a-z0-9]+\.[a-z0-9]+)\b", low)
        if m and re.search(r"\b(scale|resize|upgrade|instance class|class)\b", low):
            changes["instance_class"] = m.group(1)
    if "allocated_storage" not in changes:
        m = re.search(r"\b(?:storage|disk)\D{0,16}?(\d{2,5})\s*g[i]?b\b", low)
        if m:
            changes["allocated_storage"] = int(m.group(1))
    if "tags" not in changes:
        m = re.search(r"\btag\s+([A-Za-z][\w.-]*)\s*[=:]\s*([\w.-]+)", message)
        if m:
            changes["tags"] = {m.group(1): m.group(2)}
    if isinstance(changes.get("power"), str):
        changes["power"] = changes["power"].strip().lower()
        if changes["power"] not in ("running", "stopped"):
            changes.pop("power")
    return changes


# MOD capability map: which day-2 changes each resource family supports, and how each
# change lands on the module inputs. Power is Terraform-encoded (owner Option A) — AWS via
# aws_ec2_instance_state, GCE via desired_status; Azure has no TF-native power path.
_MODIFY_CAPS: dict[str, set[str]] = {
    "aws.ec2": {"ingress_ports", "power", "tags"},
    "gcp.vm": {"ingress_ports", "power"},
    "azure.vm": {"ingress_ports"},
    "aws.s3": {"versioning", "lifecycle_expire_days", "tags"},
    "aws.rds": {"instance_class", "allocated_storage", "tags"},
}

_AZURE_POWER_ANSWER = ("Power on/off isn't supported for Azure — use the portal for that; "
                       "create, modify, and destroy are fully supported here.")


def _apply_modification(base: dict, changes: dict) -> tuple[dict, list[str]]:
    """Merge the requested changes onto the stored inputs; return (merged, descriptions)."""
    merged = dict(base)
    desc: list[str] = []
    if "ingress_ports" in changes:
        current = [int(p) for p in (base.get("ingress_ports") or [])]
        merged["ingress_ports"] = sorted(set(current) | set(changes["ingress_ports"]))
        desc.append(f"open inbound TCP {changes['ingress_ports']}")
    if "power" in changes:
        merged["power_state"] = changes["power"]
        desc.append(f"set power state to {changes['power']} (Terraform-managed, no SDK call)")
    if "versioning" in changes:
        merged["versioning"] = bool(changes["versioning"])
        desc.append(f"turn versioning {'on' if changes['versioning'] else 'off'}")
    if "lifecycle_expire_days" in changes:
        merged["lifecycle_expire_days"] = int(changes["lifecycle_expire_days"])
        desc.append(f"expire objects after {changes['lifecycle_expire_days']} days")
    if "instance_class" in changes:
        merged["instance_class"] = str(changes["instance_class"])
        desc.append(f"scale to {changes['instance_class']}")
    if "allocated_storage" in changes:
        merged["allocated_storage"] = int(changes["allocated_storage"])
        desc.append(f"grow storage to {changes['allocated_storage']} GiB")
    if "tags" in changes:
        merged["extra_tags"] = {**(base.get("extra_tags") or {}), **dict(changes["tags"])}
        desc.append("update tags " + ", ".join(f"{k}={v}" for k, v in changes["tags"].items()))
    return merged, desc


async def _extract_inputs(settings, template, message: str, *, org_id: str | None = None,
                          user_id: str | None = None) -> dict:
    """Extract this module's parameter values from NL via Gemini, merged with free-form parsing.

    M4: "my usual region" resolves DETERMINISTICALLY from the user's standing memory — no LLM
    required — and an explicit region in the message still wins."""
    from ..schemas.workflows import parse_freeform

    inputs = parse_freeform(message)
    if org_id and re.search(r"\busual\s+(?:region|location)\b", message or "", re.IGNORECASE):
        from . import user_memory
        usual = await user_memory.lookup(org_id, user_id, "usual_region")
        if usual:
            field = "location" if template.cloud == "azure" else "region"
            inputs.setdefault(field, usual)
            log.info("cloudops.usual_region_honored", region=usual, field=field)
    gemini = get_gemini(settings)
    if gemini.enabled:
        fields = params.extraction_fields(template.key) or ", ".join(template.schema.model_fields.keys())
        system = (
            "Extract provisioning parameter values from the user's message for a Terraform module. "
            f"Look for these fields (use the exact names as JSON keys): {fields}. "
            "Respond with ONLY a JSON object; omit any field not present in the message. "
            "Normalize OS synonyms: 'ubuntu'->'ubuntu-22.04', 'amazon linux'/'al2023'->'amazon-linux-2023', "
            "'windows'->'windows-2022'. If the user asks to create/generate a key pair, use \"create\". "
            "For allowed_cidr: an IP the user wants access from (bare IP is fine, e.g. 203.0.113.7); "
            "'none'/'closed'/'keep it closed'/'skip' -> \"none\". Lists as JSON arrays."
        )
        try:
            extracted = await llm.classify_json(settings, system, message)
            clean = {k: v for k, v in extracted.items() if v not in (None, "")}
            inputs = {**clean, **inputs}  # explicit free-form key=value wins over the LLM
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.extract_failed", error=str(e))
    return inputs


def _invalid_fields(exc: Exception) -> list[tuple[str, str]]:
    """Field-level messages from a Pydantic ValidationError (for a specific clarification)."""
    try:
        return [(str(e.get("loc", ["input"])[0]), e.get("msg", "invalid")) for e in exc.errors()]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return [("input", str(exc))]


# Resource vocabulary → cloud hint (fallback only, when neither the request nor the UI names a
# cloud). ONLY cloud-branded service names are hinted (S3/RDS/VPC/EKS→aws, Storage/Resource
# Group→azure, GCS→gcp). Compute/"VM"/"ec2" is deliberately NOT hinted: a VM request with no
# named cloud is genuinely cross-cloud, so it must ask rather than silently defaulting to AWS.
_CLOUDS = {"aws", "azure", "gcp"}
_RESOURCE_CLOUD = {"s3": "aws", "rds": "aws", "vpc": "aws", "eks": "aws",
                   "resource_group": "azure", "storage": "azure", "gcs": "gcp"}


def _defaulted_dependencies(cloud: str, resource: str, validated: dict, resources: list | None) -> list[dict]:
    """DEF: dependency placements that were NOT user-specified and got a default — stated
    explicitly for the approval card so there is no invisible placement decision. The value is the
    resolved id from the plan where available, else an honest description of the default."""
    out: list[dict] = []
    r = (resource or "").lower()
    if cloud == "aws" and r in ("ec2", "vm", "instance", "server"):
        if not validated.get("vpc_id"):
            inst = next((x.get("after", {}) for x in (resources or []) if x.get("type") == "aws_instance"), {})
            sub = inst.get("subnet_id")
            out.append({"name": "VPC / subnet", "value": sub or "account default VPC + subnet",
                        "note": "no VPC specified — placing in the account's default VPC"})
    elif cloud == "gcp" and r in ("vm", "instance", "gce", "server"):
        # MS-12 (B4, by design): a slot-filled/user-named network is a REAL placement — the
        # DEP closure states its provenance; only the default placement is flagged here.
        if (validated.get("network") or "default") == "default":
            out.append({"name": "Network", "value": "default",
                        "note": "no network specified — placing in the project's 'default' VPC network"})
    elif cloud == "azure" and r in ("vm", "instance", "server"):
        if not validated.get("resource_group"):
            out.append({"name": "Resource group", "value": f"{validated.get('name', '')}-rg (auto-created)",
                        "note": "no resource group specified — a dedicated one is auto-created"})
    return out


def resolve_cloud(state: AgentState) -> tuple[str | None, str]:
    """Resolve the target cloud with explicit priority (2.1):

    (a) a cloud named in the request (router-extracted), (b) the UI cloud selector, (c) a hint
    from the resource vocabulary. If still ambiguous, return (None, reason) so the agent asks
    the user — it must NEVER silently default to AWS.
    """
    explicit = (state.get("cloud") or "").lower()
    if explicit in _CLOUDS:
        return explicit, "named in request"
    ui = (state.get("user", {}).get("cloud") or "").lower()
    if ui in _CLOUDS:
        return ui, "UI cloud selector"
    hint = _RESOURCE_CLOUD.get((state.get("resource") or "").lower())
    if hint:
        return hint, f"inferred from resource '{state.get('resource')}'"
    return None, "ambiguous"


async def _availability(settings, cloud: str, region: str, emitter) -> dict:
    """Real read-only availability/connectivity pre-check via the matching cloud SDK."""
    from ..integrations.langfuse_client import get_tracer

    async with get_tracer(settings).tool(f"{cloud}.availability",
                                         input={"cloud": cloud, "region": region}) as t:
        result = await _availability_inner(settings, cloud, region)
        t.output = result
    return result


async def _availability_inner(settings, cloud: str, region: str) -> dict:
    try:
        if cloud == "aws":
            r = aws_tool.get_aws(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "AWS credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "AWS reachable (STS)", "passed": ok, "detail": region}]}
        if cloud == "azure":
            r = azure_tool.get_azure(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "Azure credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "Azure reachable", "passed": ok}]}
        if cloud == "gcp":
            r = gcp_tool.get_gcp(settings)
            if not r.enabled:
                return {"available": True, "checks": [{"name": "GCP credentials", "passed": False, "detail": "not configured"}]}
            ok = await r.ping()
            return {"available": ok, "checks": [{"name": "GCP reachable", "passed": ok}]}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "checks": [{"name": f"{cloud} availability", "passed": False, "detail": str(e)[:160]}]}
    return {"available": True, "checks": []}


async def cloudops_plan(state: AgentState, config) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    action = state.get("action") or "create"
    region = state.get("user", {}).get("region", "us-east-1")
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))

    # ── Day-2 operations on an EXISTING resource (Phase 4): resolve it from inventory.
    # A BROAD target ("all") is not a single-resource lookup — it falls through to the
    # discovery path below, which answers with live per-cloud counts AND the full inventory
    # listing (Phase 7 / BUG-04).
    target = state.get("target")
    if action == "read" and target and not inventory.is_broad_ref(target):
        return await _read_resource(state, config, target)
    if action == "modify":
        return await _modify_resource(state, config, target)
    # Destroy is a DAY-2 operation on an inventoried resource — it resolves its target from
    # the inventory, confirms it via the approval gate, and tears down that resource's OWN
    # state workspace. It never enters the create-style parameter collection (Phase 8 / N-08:
    # the old path collected create-params and destroyed whatever shared state contained).
    if action == "destroy":
        return await _destroy_resource(state, config, target)

    # ── Read path: discovery only, no Terraform. Runs BEFORE cloud resolution because a
    # read-only question must never hard-fail on cloud ambiguity — it can answer across every
    # cloud the question names (or all configured ones). (Phase 7 / BUG-01, BUG-04.)
    if action == "read":
        await timing.start_step(run_id, "cloudops_agent")
        r = await _read_path(state, config)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return r

    # 2.1 — resolve the target cloud with explicit priority; never silently default to AWS.
    cloud, cloud_reason = resolve_cloud(state)
    if cloud is None:
        await emitter.step(2, "Cloud ambiguous — asking user")
        return {"needs_clarification": True, "needs_change": False,
                "clarification": "Which cloud should I target — **AWS**, **Azure**, or **GCP**? "
                                 "I couldn't tell from your request or the workspace cloud selector."}
    resource = (state.get("resource") or "").lower()
    log.info("cloudops.cloud_resolved", cloud=cloud, reason=cloud_reason, resource=resource, run_id=run_id)
    await emitter.step(2, f"Target cloud · {cloud.upper()} ({cloud_reason})")

    # 2.2 — select the cloud-specific template. No cross-cloud fallback: if this cloud has no
    # approved module for the resource, clarify honestly (don't provision on a different cloud).
    template = templates.select(cloud, resource)
    if template is None:
        await emitter.step(3, f"No approved {cloud}/{resource or '?'} module")
        supported = ", ".join(sorted(f"{t.cloud}/{t.resource}" for t in templates.TEMPLATES))
        return {"needs_clarification": True, "needs_change": False, "cloud": cloud,
                "clarification": f"I resolved the target cloud to **{cloud.upper()}**, but there's no approved "
                                 f"Terraform module for **{cloud}/{resource or 'that resource'}** yet, so I can't plan "
                                 f"it — and I won't provision it on another cloud. Currently supported: {supported}."}

    await emitter.step(3, f"Selected workflow · {template.key} {template.version}")

    # ── Interactive parameter collection (3.1–3.3) — CREATE only: read/modify/destroy have
    # all returned above, so this flow can only ever produce a create plan (Phase 8 / N-08).
    session_id = state.get("session_id")
    pending_rec: dict = {}
    if state.get("collecting") and session_id:
        pending_rec = await params.load_pending(session_id) or {}

    await timing.start_step(run_id, "cloudops_agent")

    def _pending_record(collected: dict) -> dict:
        return {"template": template.key, "cloud": cloud, "resource": resource, "action": "create",
                "intent": state.get("intent"), "collected": collected,
                "snow_id": state.get("snow_id"), "context_id": context_id}

    prior: dict = pending_rec.get("collected", {})
    extracted = await _extract_inputs(settings, template, state["message"],
                                      org_id=state.get("org_id"),
                                      user_id=state.get("user", {}).get("user_id"))
    collected = {**prior, **{k: v for k, v in extracted.items() if v not in (None, "", [])}}
    # GCP resources default the project to the configured one (never asked).
    if cloud == "gcp" and not collected.get("project"):
        collected["project"] = settings.google_cloud_project

    # Missing decision-critical params → ask for exactly those (never for defaulted VPC/subnet).
    missing = params.missing_required(template.key, collected)
    if missing:
        req = params.request_payload(template.key, collected)
        msg = params.summary_text(template.key, collected)
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        await emitter.params(req)
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "param_request": req,
                "parsed_inputs": collected, "confidentiality": {"level": cc.level, "score": cc.score}}

    # All required present → validate the concrete Terraform variables against the module schema.
    if session_id:
        await params.clear_pending(session_id)
    try:
        validated = template.schema(**params.to_tf_vars(template.key, collected)).model_dump()
    except Exception as e:  # noqa: BLE001 - Pydantic ValidationError → per-field clarification, no plan
        bad = _invalid_fields(e)
        labels = {p.name: p.label for p in params.specs_for(template.key)}
        for fname, _ in bad:
            collected.pop(fname, None)  # drop the invalid value so it's re-asked specifically
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        detail = "; ".join(f"**{labels.get(f, f)}** — {m}" for f, m in bad) or str(e)
        msg = f"That value didn't validate: {detail}. Please send a valid value."
        await emitter.params(params.request_payload(template.key, collected))
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="failed", error=str(e))
        await cg.add_step(order=1, name="validate_inputs", agent="cloudops", tool="pydantic", status="failed")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── S3 names are GLOBALLY unique — read-only HeadBucket precheck so the user isn't burned
    # by a 409 at apply time (Phase 7 / BUG-07). Best-effort: unknown ⇒ proceed to plan.
    if template.key == "aws.s3" and aws_tool.get_aws(settings).enabled:
        taken = await aws_tool.get_aws(settings).bucket_taken(validated["bucket_name"])
        if taken:
            bad_name = validated["bucket_name"]
            collected.pop("bucket_name", None)
            if session_id:
                await params.save_pending(session_id, _pending_record(collected))
            msg = (f"The bucket name **`{bad_name}`** is already taken — S3 bucket names are "
                   "globally unique across ALL AWS customers, so common names are long gone. "
                   "Pick a more distinctive one (e.g. prefix it with your org or project, "
                   "like `acme-payments-logs`) and send it over.")
            await emitter.step(4, f"Bucket name “{bad_name}” is taken — asking for another")
            await emitter.params(params.request_payload(template.key, collected))
            await emitter.token(msg)
            cc = classify(msg)
            await emitter.confidentiality(cc.level, cc.score)
            await timing.end_step(run_id, "cloudops_agent", status="done")
            return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                    "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                    "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── Availability (real SDK reads) ──
    await emitter.step(4, f"Queried {cloud.upper()} · {region}")
    avail = await _availability(settings, cloud, region, emitter)
    for c in avail["checks"]:
        await emitter.console("stdout", f"availability: {c['name']} = {'ok' if c['passed'] else 'n/a'} {c.get('detail','')}")
    await timing.end_step(run_id, "cloudops_agent", status="done")

    # ── Terraform plan ── (fixed reviewed module; user inputs passed strictly as -var)
    workspace = template.workspace
    tf_vars = validated

    # Per-resource state isolation (Phase 8 / N-08): every create plans/applies inside its
    # OWN Terraform workspace, so it can never reconcile — and destroy/replace — a resource
    # created earlier in the same module ("create deleted my previous instance").
    res_name = inventory.name_from_inputs(validated, template.resource)
    tf_state_ws = state_slug(res_name)

    # Same-name loophole (Senior Reviewer): an identical name would map to the SAME state
    # workspace and re-share state. A create never reuses an active resource's name.
    dup = [m for m in await inventory.list_active(state["org_id"])
           if m["name"] == res_name and m["workspace"] == template.workspace]
    if dup:
        collected.pop(next((p.name for p in params.specs_for(template.key)
                            if p.name in ("name", "bucket_name", "identifier", "cluster_name",
                                          "account_name")), "name"), None)
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        msg = (f"You already have an active resource named **{res_name}** (created "
               f"{(dup[0].get('created_at') or '')[:16]}). A create never touches an existing "
               "resource — pick a different name, or say "
               f"“destroy {res_name}” first if you want to replace it.")
        await emitter.step(4, f"Name “{res_name}” already active — asking for another")
        await emitter.params(params.request_payload(template.key, collected))
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": msg, "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # ── DEP: dependency closure (strict order: named → world model → stated default →
    # create-first DAG). An ambiguous parent ASKS with the real candidates; a missing required
    # parent yields an ordered create-first plan (executed by the executive loop, U6) — the
    # single-step path proceeds with the resolved inputs + provenance notes for the card.
    closure = dependency.resolve_closure(
        template.key, validated,
        await inventory.list_active(state["org_id"]), message=state.get("message", ""))
    if closure.status == "ask":
        if session_id:
            await params.save_pending(session_id, _pending_record(collected))
        await emitter.step(4, "Placement is ambiguous — asking")
        await emitter.token(closure.question)
        cc = classify(closure.question)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "collecting": True,
                "cloud": cloud, "resource": resource, "answer": closure.question,
                "parsed_inputs": collected,
                "confidentiality": {"level": cc.level, "score": cc.score}}
    if closure.status == "dag":
        if settings.aegisops_exec_loop == "on":
            # U6: hand the create-first DAG to the Governed Executive Loop — per-step plans +
            # ONE whole-DAG approval; execution happens post-approval in the execute node.
            from . import exec_loop
            return await exec_loop.plan_goal_dag(state, config, closure.dag)
        steps_txt = " → ".join(f"{i+1}) {s['template_key']}"
                               f" “{s['inputs'].get('name') or s['inputs'].get('cluster_name') or s['inputs'].get('account_name') or ''}”"
                               for i, s in enumerate(closure.dag))
        msg = (f"**{template.key}** needs a {closure.dag[0]['provides']} that doesn't exist yet. "
               f"Ordered plan (parents first): {steps_txt} — the child is wired to the parent's "
               "real outputs. Multi-step execution runs through the governed executive loop; "
               "each plan is applied only after your approval.")
        await emitter.step(4, "Create-first plan drafted (dependency closure)")
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        await timing.end_step(run_id, "cloudops_agent", status="done")
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "goal_dag": closure.dag, "cloud": cloud, "resource": resource,
                "confidentiality": {"level": cc.level, "score": cc.score}}
    validated = closure.inputs
    # MODSEED: environment-aware defaults (e.g. NLB deletion_protection ON for Production) —
    # resolved after validation, stated on the card, never silent; explicit choices win.
    env_notes = templates.apply_env_defaults(template.key, validated,
                                             state.get("user", {}).get("env"))
    tf_vars = validated

    mode = "apply"
    await emitter.step(4, f"Ran terraform plan · state {tf_state_ws}")
    await timing.start_step(run_id, "planner", tool="terraform")
    runner = TerraformRunner(workspace, settings, state_workspace=tf_state_ws, run_id=run_id)

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(tf_vars, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        # Classify the provider failure and explain it in plain English (Phase 7 / BUG-05) —
        # the raw trace stays in the Logs tab; the conversation gets cause + next step.
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="plan")
        # U7: one-click retry-with-fix — a genuine new turn with only the fix applied.
        retry = provider_errors.suggest_retry(failure, state.get("message", ""), cloud=cloud,
                                              current_region=validated.get("region")
                                              or validated.get("location") or region)
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error",
                            retriable=True, retry=retry)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        await cg.add_step(order=2, name="terraform_plan", agent="cloudops", tool="terraform", status="failed")
        await cg.update_step(order=2, status="failed", error=str(e))
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None,
                            "retry": retry}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # Action-vs-operation HARD GUARD (Phase 8 / N-08): the plan about to reach the approval
    # gate must be a pure create. Any destroy/replace in a create plan is blocked here —
    # whatever the classifier or Terraform state did.
    violation = plan_guard.check_plan_actions("create", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="create",
                  summary=plan["summary"])
        await emitter.step(9, "Safety guard · create plan contained destroys — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        await emitter.token(violation)
        cc = classify(violation)
        await emitter.confidentiality(cc.level, cc.score)
        await cg.add_step(order=2, name="plan_guard", agent="cloudops", tool="plan_guard", status="failed")
        return {"needs_change": False, "approval_status": "not_required", "answer": violation,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "blocked_by_guard", "error": violation}}

    await timing.start_step(run_id, "policy_evaluation")
    policy_checks = template.policy_fn(validated, runner.planned_resources())  # U1: over the real plan
    await timing.end_step(run_id, "policy_evaluation", status="done",
                          result={"passed": sum(1 for p in policy_checks if p["passed"]), "total": len(policy_checks)})
    # DEF: surface any silently-defaulted dependency placement on the approval card — plus the
    # DEP resolver's provenance ("using existing vpc … from the world model").
    defaults = _defaulted_dependencies(cloud, template.resource, validated, runner.planned_resources())
    defaults += [{"name": "Dependency resolution", "value": n, "note": ""} for n in closure.notes]
    defaults += [{"name": "Environment default", "value": n, "note": ""} for n in env_notes]
    plan_json = {"summary": plan["summary"], "diff": plan["diff"], "workspace": template.workspace,
                 "policy_checks": policy_checks, "mode": mode, "state_workspace": tf_state_ws,
                 "defaults": defaults}

    # Confidentiality over the plan + inputs.
    c = classify(json.dumps({"inputs": validated, "plan": plan["summary"]}))
    await emitter.confidentiality(c.level, c.score)

    reasoning = [
        {"title": "Interpreted intent", "conf": f"{int(state.get('intent_confidence', 0) * 100)}%",
         "body": f"{action} {template.resource} on {cloud} — {state.get('routing_reason','')}"},
        {"title": "Workflow selection", "conf": "",
         "body": f"Selected {template.key} {template.version} ({template.description}). Curated, org-approved module."},
        {"title": "Plan", "conf": "",
         "body": f"+{plan['summary']['add']} ~{plan['summary']['change']} -{plan['summary']['destroy']}; "
                 f"{sum(1 for p in policy_checks if p['passed'])}/{len(policy_checks)} policy checks passed."},
    ]
    await emitter.analysis(summary=f"Drafted a Terraform plan for {template.key}; awaiting approval.", cards=reasoning)

    # Context graph
    try:
        await cg.set_workflow(workflow=template.key, version=template.version, template=template.workspace or "generic", inputs=validated)
        await cg.add_step(order=2, name="terraform_plan", agent="cloudops", tool="terraform", status="done")
        await cg.update_step(order=2, status="done", result=plan["summary"])
        await cg.add_evidence(kind="terraform_plan", ref=template.key, detail=plan["summary"])
    except Exception as e:  # noqa: BLE001
        log.warning("cloudops.cg_failed", error=str(e))

    interrupt_payload = {
        "kind": "approval", "runId": state["run_id"], "workflow": template.key,
        "plan": plan_json, "diff": plan["diff"], "policyChecks": policy_checks,
        "mode": mode, "cloud": cloud, "resource": template.resource, "defaults": defaults,
    }
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)

    return {
        "workflow": template.key, "workflow_version": template.version,
        "cloud": cloud, "resource": resource, "state_workspace": tf_state_ws,
        "parsed_inputs": validated, "plan_json": plan_json, "diff": plan["diff"],
        "policy_checks": policy_checks, "dependencies": avail["checks"],
        "execution_mode": mode, "needs_change": True, "approval_status": "pending",
        "confidentiality": {"level": c.level, "score": c.score},
        "reasoning_cards": reasoning, "interrupt_payload": interrupt_payload,
        "answer": f"Drafted a {template.key} plan (+{plan['summary']['add']} ~{plan['summary']['change']} "
                  f"-{plan['summary']['destroy']}). Review and approve to apply.",
    }


# Which resource kind a read question is about (normalizes per-cloud synonyms).
_READ_KINDS = {
    "vm": {"ec2", "vm", "gce", "instance", "compute", "server", "machine"},
    "storage": {"s3", "gcs", "storage", "bucket", "blob", "object_storage", "storage_account"},
    "db": {"rds", "database", "db", "postgres", "postgresql", "cloudsql", "sql", "mysql"},
    "k8s": {"eks", "aks", "gke", "k8s", "kubernetes", "cluster"},
    "network": {"vpc", "network", "subnet"},
}
_CLOUD_WORDS = {"aws": r"\baws\b|\bamazon\b", "azure": r"\bazure\b", "gcp": r"\bgcp\b|\bgoogle\b"}


def _read_kind(resource: str) -> str:
    r = (resource or "").lower()
    return next((k for k, words in _READ_KINDS.items() if r in words), "any")


async def _discover_aws(settings, kind: str, region: str) -> list[str]:
    r = aws_tool.get_aws(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        inst = await r.list_instances(region)
        running = [i for i in inst if i.get("state") == "running"]
        out.append(f"{len(running)} running EC2 instance(s)"
                   + (f" ({', '.join(i['name'] or i['id'] for i in running[:5])})" if running else "")
                   + (f" of {len(inst)} total" if len(inst) != len(running) else ""))
    if kind in ("storage", "any"):
        buckets = await r.list_buckets()
        out.append(f"{len(buckets)} S3 bucket(s)"
                   + (f" ({', '.join(b['name'] for b in buckets[:5])}{'…' if len(buckets) > 5 else ''})" if buckets else ""))
    if kind in ("db", "any"):
        out.append(f"{len(await r.list_databases(region))} RDS instance(s)")
    if kind == "k8s":
        out.append(f"{len(await r.list_eks_clusters(region))} EKS cluster(s)")
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_vpcs(region))} VPC(s)")
    return out


async def _discover_azure(settings, kind: str) -> list[str]:
    r = azure_tool.get_azure(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        vms = await r.list_vms()
        out.append(f"{len(vms)} VM(s)"
                   + (f" ({', '.join(v['name'] for v in vms[:5])})" if vms else ""))
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_vnets())} VNet(s)")
    if kind in ("storage", "db", "k8s") and not out:
        out.append(f"live {kind} listing isn't wired for Azure yet — the AegisOps inventory below covers what I created")
    return out


async def _discover_gcp(settings, kind: str) -> list[str]:
    r = gcp_tool.get_gcp(settings)
    if not r.enabled:
        return ["credentials not configured"]
    out: list[str] = []
    if kind in ("vm", "any"):
        inst = await r.list_all_instances()
        running = [i for i in inst if (i.get("status") or "").upper() == "RUNNING"]
        out.append(f"{len(running)} running Compute instance(s)"
                   + (f" ({', '.join(i['name'] for i in running[:5])})" if running else "")
                   + (f" of {len(inst)} total" if len(inst) != len(running) else ""))
    if kind in ("network", "any"):
        out.append(f"{len(await r.list_networks())} network(s)")
    if kind in ("storage", "db", "k8s") and not out:
        out.append(f"live {kind} listing isn't wired for GCP yet — the AegisOps inventory below covers what I created")
    return out


async def _read_path(state, config) -> dict:
    """Read-only account discovery — real SDK reads, no Terraform, no approval needed.

    Multi-cloud aware (Phase 7): answers for every cloud named in the question, else the
    resolved/selected cloud, else every configured cloud. Counts the resource kind actually
    asked about (instances/buckets/databases/clusters/VPCs), and always includes what the
    AegisOps inventory recorded — never a destructive or provisioning workflow.
    """
    emitter = emitter_of(config)
    settings = get_settings()
    message = (state.get("message") or "").lower()
    region = state.get("user", {}).get("region", "us-east-1")
    kind = _read_kind(state.get("resource"))

    clouds = [c for c, pat in _CLOUD_WORDS.items() if re.search(pat, message)]
    if not clouds:
        resolved, _why = resolve_cloud(state)
        clouds = [resolved] if resolved else [c for c, tool in
                  (("aws", aws_tool.get_aws(settings)), ("azure", azure_tool.get_azure(settings)),
                   ("gcp", gcp_tool.get_gcp(settings))) if tool.enabled] or ["aws"]

    await emitter.step(3, f"Querying {', '.join(c.upper() for c in clouds)} · read-only")
    sections: list[str] = []
    for cloud in clouds:
        try:
            found = await (_discover_aws(settings, kind, region) if cloud == "aws"
                           else _discover_azure(settings, kind) if cloud == "azure"
                           else _discover_gcp(settings, kind))
            sections.append(f"**{cloud.upper()}**: " + ", ".join(found))
            for f in found:
                await emitter.console("stdout", f"discovery[{cloud}]: {f}")
        except Exception as e:  # noqa: BLE001 - one cloud failing must not sink the others
            log.warning("cloudops.discovery_failed", cloud=cloud, error=str(e))
            f = provider_errors.classify_provider_error(str(e))
            sections.append(f"**{cloud.upper()}**: discovery failed — "
                            + (f"{f.title}. {f.next_step}" if f else str(e)[:140]))

    # What AegisOps itself provisioned (the inventory) — always part of the answer. For a
    # broad "did I create any resources" question, render the full grouped listing.
    try:
        mine = await inventory.list_active(state["org_id"], clouds=clouds)
        if inventory.is_broad_ref(state.get("target")) or intent_guard.is_broad_inventory_question(message):
            # Broad = everything created, across ALL clouds (not just the ones asked about).
            sections.append("\n" + _render_inventory_list(await inventory.list_active(state["org_id"])))
        elif mine:
            names = ", ".join(f"{m['name']} ({m['cloud']} {m['resource_type']})" for m in mine[:8])
            sections.append(f"**Provisioned by AegisOps**: {len(mine)} active — {names}")
        else:
            sections.append("**Provisioned by AegisOps**: none recorded for these clouds")
    except Exception as e:  # noqa: BLE001
        log.warning("cloudops.inventory_list_failed", error=str(e))

    text = "\n".join(sections)
    await emitter.token(text)
    c = classify(text)
    await emitter.confidentiality(c.level, c.score)
    await emitter.analysis(
        summary="Read-only discovery via cloud SDK reads plus the AegisOps inventory — no "
                "infrastructure was changed and no approval was needed.",
        cards=[{"title": "Read-only query", "conf": "",
                "body": f"kind={kind} · clouds={', '.join(clouds)} · region={region}"}])
    return {"needs_change": False, "approval_status": "not_required", "answer": text,
            "confidentiality": {"level": c.level, "score": c.score}}


def _render_inventory_list(matches: list[dict]) -> str:
    """Human summary of every active inventoried resource, grouped by cloud (BUG-04)."""
    if not matches:
        return ("I haven't provisioned any resources in this workspace yet — nothing is recorded "
                "in the inventory. (Failed applies leave no active resource, and destroyed ones "
                "are removed from this list.) Ask me to create something — e.g. "
                "“create an EC2 instance in AWS”.")
    by_cloud: dict[str, list[dict]] = {}
    for m in matches:
        by_cloud.setdefault(m["cloud"], []).append(m)
    lines = [f"I've provisioned **{len(matches)}** active resource(s):"]
    for cloud in sorted(by_cloud):
        lines.append(f"\n**{cloud.upper()}**")
        for m in by_cloud[cloud]:
            bits = [f"• **{m['name']}** — {m['resource_type']}"]
            if m.get("provider_id"):
                bits.append(f"(`{m['provider_id']}`)")
            if m.get("region"):
                bits.append(f"· {m['region']}")
            created = (m.get("created_at") or "")[:16].replace("T", " ")
            if created:
                bits.append(f"· created {created}")
            lines.append(" ".join(bits))
    lines.append("\nAsk about any of them by name for full details (IPs, VPC, ports, …).")
    return "\n".join(lines)


async def _read_resource(state: AgentState, config, target: str) -> dict:
    """Day-2 READ: return a specific inventoried resource's real recorded values (reconciled)."""
    emitter = emitter_of(config)
    settings = get_settings()
    org_id = state["org_id"]
    await emitter.step(2, f"Looking up “{target}” in inventory")
    matches, kind = await inventory.resolve(org_id, target)

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # Broad inventory question ("did I create any resources…") → list EVERYTHING active,
    # grouped by cloud — never the literal-match refusal (Phase 7 / BUG-04, screenshots 14/16).
    if kind == "all":
        await emitter.step(3, f"Inventory · {len(matches)} active resource(s)")
        await emitter.analysis(
            summary="Listed every active resource recorded in the AegisOps inventory for this "
                    "workspace (successful applies only; failed/destroyed runs leave no active resource).",
            cards=[{"title": "Inventory listing", "conf": "", "body": f"{len(matches)} active resource(s)"}])
        return await _say(_render_inventory_list(matches))

    if not matches:
        return await _say(f"I couldn't find a resource matching “{target}” in what I've provisioned for you. "
                          "Tell me its exact name, or create it first — I won't guess.")
    if len(matches) > 1:
        return await _say(f"More than one resource matches “{target}”: "
                          f"{', '.join(m['name'] for m in matches)}. Which one do you mean?")
    res = await inventory.reconcile(matches[0], settings)
    await emitter.step(3, f"Recalled {res['name']} · {res.get('provider_id') or ''}")
    attrs = res.get("attributes") or {}
    inputs = res.get("inputs") or {}
    lines = [f"**{res['name']}** — {res['cloud']} {res['resource_type']}"
             + (f" (`{res['provider_id']}`)" if res.get("provider_id") else "") + ":"]
    # Size/type comes from the validated inputs the resource was created with (asked-for
    # attribute in screenshot 6 — "instance size" — was previously never surfaced).
    size = inputs.get("instance_type") or inputs.get("machine_type") or inputs.get("size") or inputs.get("tier")
    if size:
        lines.append(f"• Size / type: `{size}`")
    if res.get("region"):
        lines.append(f"• Region: `{res['region']}`")
    for label, key in [("VPC", "vpc_id"), ("Subnet", "subnet_id"), ("Private IP", "private_ip"),
                       ("Public IP", "public_ip"), ("Public DNS", "public_dns"),
                       ("Security group", "security_group_id"), ("Key pair", "key_name"), ("State", "state")]:
        if attrs.get(key):
            lines.append(f"• {label}: `{attrs[key]}`")
    if attrs.get("ingress_ports"):
        lines.append(f"• Open inbound ports: {attrs['ingress_ports']}")
    if res.get("status") != "active":
        lines.append(f"• ⚠️ Status: {res['status']}")
    # Relationships come from the context graph (never inferred): who provisioned it.
    prov = await inventory.provenance(provider_id=res.get("provider_id"), name=res["name"])
    if prov and (prov.get("run_id") or prov.get("session_id")):
        lines.append(f"• Provisioned by run `{str(prov.get('run_id') or '')[:8]}`"
                     + (f" in session `{str(prov.get('session_id'))[:8]}`" if prov.get("session_id") else "")
                     + " (context graph)")
    msg = "\n".join(lines)
    await emitter.token(msg)
    cc = classify(msg)
    await emitter.confidentiality(cc.level, cc.score)
    await emitter.analysis(
        summary=f"Recalled “{res['name']}” from the provisioned-resource inventory (matched by {kind}); "
                "these are this resource's real recorded attributes, not a generic account discovery.",
        cards=[{"title": "Resolved resource", "conf": "",
                "body": f"{res['name']} · {res.get('provider_id')} · workspace {res['workspace']}"}])
    return {"needs_change": False, "approval_status": "not_required", "answer": msg,
            "cloud": res["cloud"], "resource": res["resource_type"],
            "confidentiality": {"level": cc.level, "score": cc.score}}


async def _world_model_impact_check(org_id: str, *, provider_id: str | None,
                                    name: str | None) -> tuple[dict, list[dict]]:
    """(policy-check row, dependents) for the destroy card — D3 impact gate.

    passed=True only when the world model was actually consulted and found no active dependent;
    dependents → passed=False with a named detail; an unreachable graph → evaluated=False
    (pending), never a silent pass."""
    from ..graph_db import world_model
    try:
        dependents = await world_model.impact_of(org_id, provider_id=provider_id, name=name)
    except Exception as e:  # noqa: BLE001 — the graph being down must not fake a pass
        log.warning("cloudops.impact_check_unavailable", error=str(e))
        return ({"name": "No dependent resources (world model)", "passed": None,
                 "evaluated": False, "detail": "world model unreachable — not evaluated"}, [])
    if dependents:
        detail = ", ".join(f"{d['name'] or d['provider_id']} ({d['type'] or d['kind']})"
                           for d in dependents[:6])
        return ({"name": "No dependent resources (world model)", "passed": False,
                 "evaluated": True,
                 "detail": f"{len(dependents)} active dependent(s): {detail}"}, dependents)
    return ({"name": "No dependent resources (world model)", "passed": True,
             "evaluated": True, "detail": "no active dependents"}, [])


async def _destroy_resource(state: AgentState, config, target: str | None) -> dict:
    """Day-2 DESTROY (Phase 8 / N-08): resolve the target from the INVENTORY, confirm it via
    the approval gate, and tear down that resource's OWN Terraform state workspace.

    Never enters parameter collection, never plans in a shared state, and the plan is hard-
    guarded to contain only delete actions. Resources AegisOps didn't create are refused
    honestly (we won't guess at infrastructure we don't own)."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    org_id = state["org_id"]
    message = state.get("message", "")

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    # Destroy always requires the user's own destructive wording — belt-and-suspenders under
    # the router guard (a mirror-redirected destroy carries the verb by construction).
    if not intent_guard.explicitly_destructive(message):
        return await _say("This came through as a **destroy**, but your message doesn't explicitly "
                          "ask to tear anything down, so I stopped — nothing was changed. If you do "
                          "want it destroyed, say so explicitly (e.g. “destroy the VM sai-test”).")

    ref = (target or "").strip()
    # U7: "__last_applied__" — the undo fast-path's target. Resolve to the most recent ACTIVE
    # resource THIS conversation applied; nothing applied here → refuse honestly.
    if ref == "__last_applied__":
        last = await inventory.last_applied(org_id, state.get("session_id"))
        if not last:
            return await _say("There's nothing to undo — this conversation hasn't applied any "
                              "infrastructure that is still active. Name a resource explicitly "
                              "if you want something torn down.")
        ref = last["name"]
        await emitter.console("stdout", f"undo → last applied in this conversation: {ref}")
    await emitter.step(2, f"Resolving “{ref or state.get('resource') or 'resource'}” for destruction")
    matches, kind = await inventory.resolve(org_id, ref or (state.get("resource") or ""))
    if kind == "all":
        names = ", ".join(m["name"] for m in matches[:8]) or "none recorded"
        return await _say("I don't bulk-destroy in one shot — that's how accidents happen. "
                          f"Tell me exactly which resource to tear down. Active: {names}.")
    if not matches:
        return await _say(f"I couldn't find “{ref or 'that resource'}” among the resources I've "
                          "provisioned, so there's nothing I can safely destroy. I only tear down "
                          "infrastructure I created (it's tracked in my inventory with its own "
                          "Terraform state).")
    if len(matches) > 1:
        return await _say("More than one resource matches: "
                          f"{', '.join(m['name'] for m in matches)}. Which one should I destroy?")
    res = matches[0]
    if kind == "recent":
        # Fuzzy reference ("the vm I created") — only proceed when it's genuinely unambiguous.
        same_type = [m for m in await inventory.list_active(org_id)
                     if m["resource_type"] == res["resource_type"]]
        if len(same_type) > 1:
            return await _say(f"You have {len(same_type)} {res['resource_type']} resources: "
                              f"{', '.join(m['name'] for m in same_type)}. "
                              "Name the one to destroy — I won't guess on a teardown.")

    template = templates.select(res["cloud"], res["resource_type"])
    if template is None:
        return await _say(f"I can't destroy {res['name']} — its module ({res['cloud']}/"
                          f"{res['resource_type']}) is no longer in the approved catalog.")

    await emitter.step(3, f"Planning teardown of {res['name']}"
                          + (f" · state {res['state_workspace']}" if res.get("state_workspace") else ""))
    await timing.start_step(run_id, "cloudops_agent")
    await timing.end_step(run_id, "cloudops_agent", status="done")
    await timing.start_step(run_id, "planner", tool="terraform")
    runner = TerraformRunner(res["workspace"] or template.workspace, settings,
                             state_workspace=res.get("state_workspace"), run_id=run_id)
    base = dict(res.get("inputs") or {})
    try:
        tf_vars = template.schema(**base).model_dump()
    except Exception:  # noqa: BLE001 - schema drift must never block a teardown
        tf_vars = base

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(tf_vars, destroy=True, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="destroy plan")
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error", retriable=True)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # HARD GUARD: a destroy plan may contain only deletes (Phase 8 / N-08).
    violation = plan_guard.check_plan_actions("destroy", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="destroy", summary=plan["summary"])
        await emitter.step(9, "Safety guard · destroy plan contained creates — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        return await _say(violation)
    if plan["summary"]["destroy"] == 0:
        return await _say(f"Terraform found nothing to tear down for **{res['name']}** — its state "
                          "is already empty (it may have been destroyed outside AegisOps). I've left "
                          "everything untouched; say “is it created?” to reconcile its status.")

    # D3: world-model impact gate — "what depends on this?" answered from the real dependency
    # edges before the human decides. Dependents make the check FAIL on the card (the human can
    # still approve — but never unwarned). Best-effort: an unreachable graph yields an honest
    # not-evaluated check, never a silent pass.
    impact_check, dependents = await _world_model_impact_check(
        org_id, provider_id=res.get("provider_id"), name=res["name"])
    plan_json = {"summary": plan["summary"], "diff": plan["diff"],
                 "workspace": res["workspace"] or template.workspace,
                 "state_workspace": res.get("state_workspace"),
                 "policy_checks": [impact_check], "mode": "destroy"}
    cc = classify(f"destroy {res['name']}")
    await emitter.confidentiality(cc.level, cc.score)
    reasoning = [{"title": "Teardown target", "conf": "",
                  "body": f"{res['name']} · {res['cloud']} {res['resource_type']} · "
                          f"{res.get('provider_id') or 'no provider id'} — resolved from the inventory; "
                          f"plan destroys {plan['summary']['destroy']} resource(s), creates none."}]
    if template.destroy_note:
        # MODSEED: the module's honest deletion semantics, stated where the human decides.
        reasoning.append({"title": "Deletion semantics", "conf": "", "body": template.destroy_note})
    if dependents:
        dep_list = ", ".join(f"{d['name'] or d['provider_id']} ({d['type'] or d['kind']})"
                             for d in dependents[:6])
        reasoning.append({"title": "⚠ Dependent resources (world model)", "conf": "",
                          "body": f"Destroying {res['name']} impacts {len(dependents)} active "
                                  f"resource(s) that depend on it: {dep_list}. They may break "
                                  "or be orphaned."})
        await emitter.console("stderr",
                              f"world-model impact: {len(dependents)} active dependent(s) — {dep_list}")
    await emitter.analysis(summary=f"Planned the teardown of {res['name']}; awaiting approval.", cards=reasoning)
    interrupt_payload = {"kind": "approval", "runId": state["run_id"], "workflow": template.key,
                         "plan": plan_json, "diff": plan["diff"], "policyChecks": [],
                         "mode": "destroy", "cloud": res["cloud"], "resource": res["resource_type"]}
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)
    return {"workflow": template.key, "workflow_version": template.version, "cloud": res["cloud"],
            "resource": res["resource_type"], "state_workspace": res.get("state_workspace"),
            "parsed_inputs": tf_vars, "plan_json": plan_json, "diff": plan["diff"],
            "policy_checks": [], "execution_mode": "destroy", "needs_change": True,
            "approval_status": "pending", "reasoning_cards": reasoning,
            "interrupt_payload": interrupt_payload,
            "confidentiality": {"level": cc.level, "score": cc.score},
            "answer": f"Planned the teardown of **{res['name']}** ({plan['summary']['destroy']} resource(s) "
                      "to destroy, nothing else touched). Review and approve to proceed."}


async def _modify_resource(state: AgentState, config, target: str | None) -> dict:
    """Day-2 MODIFY: change an inventoried resource via its module + variables + approval gate."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    org_id = state["org_id"]

    async def _say(msg: str) -> dict:
        await emitter.token(msg)
        cc = classify(msg)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": msg,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    await emitter.step(2, f"Resolving “{target or 'resource'}” for modification")
    matches, _kind = await inventory.resolve(org_id, target)
    if not matches:
        return await _say(f"I couldn't find a resource matching “{target or 'that'}” to modify. "
                          "Tell me its exact name, or create it first — I won't invent one.")
    if len(matches) > 1:
        return await _say(f"More than one resource matches “{target}”: "
                          f"{', '.join(m['name'] for m in matches)}. Which should I modify?")
    res = matches[0]
    template = templates.select(res["cloud"], res["resource_type"])
    caps = _MODIFY_CAPS.get(template.key) if template else None
    if template is None or caps is None:
        supported = ", ".join(sorted(_MODIFY_CAPS))
        return await _say(f"Modifying {res['cloud']}/{res['resource_type']} isn't supported yet. "
                          f"Day-2 modify currently covers: {supported}.")

    changes = await _extract_modification(settings, state["message"])
    # Owner Option A: Azure has no Terraform-native power path — say so honestly, never
    # fall back to an SDK mutation.
    if changes.get("power") and template.key == "azure.vm":
        return await _say(_AZURE_POWER_ANSWER)
    unsupported = sorted(set(changes) - caps)
    if changes and unsupported:
        return await _say(f"On {template.key} I can change: {', '.join(sorted(caps))} — "
                          f"not {', '.join(unsupported)}. Rephrase with a supported change "
                          f"for **{res['name']}**.")
    if not changes:
        hints = {"aws.ec2": "e.g. “stop web-01”, “add inbound port 8501 to web-01”, or “tag env=prod”",
                 "gcp.vm": "e.g. “start web-01” or “add inbound port 8080”",
                 "azure.vm": "e.g. “add inbound ports 8501, 8502”",
                 "aws.s3": "e.g. “turn versioning off”, “expire objects after 30 days”, or “tag env=prod”",
                 "aws.rds": "e.g. “scale to db.t3.large”, “grow storage to 100 GiB”, or “tag env=prod”"}
        return await _say(f"What change should I make to **{res['name']}**? "
                          f"{hints.get(template.key, '')}")

    base = dict(res.get("inputs") or {})
    merged, change_desc = _apply_modification(base, changes)
    try:
        validated = template.schema(**merged).model_dump()
    except Exception as e:  # noqa: BLE001
        return await _say(f"Couldn't build a valid modification for {res['name']}: {e}")
    change_text = "; ".join(change_desc)

    await emitter.step(3, f"Planning day-2 change on {res['name']}: {change_text}")
    await timing.start_step(run_id, "cloudops_agent")
    await timing.end_step(run_id, "cloudops_agent", status="done")
    await timing.start_step(run_id, "planner", tool="terraform")
    # Modify runs against the resource's OWN state workspace (Phase 8 / N-08).
    runner = TerraformRunner(res["workspace"] or template.workspace, settings,
                             state_workspace=res.get("state_workspace"), run_id=run_id)

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(validated, on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await timing.end_step(run_id, "planner", status="failed", error=str(e))
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode="plan")
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error", retriable=True)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score},
                "outcome": {"status": "plan_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None}}
    await timing.end_step(run_id, "planner", status="done", result=plan["summary"])

    # HARD GUARD: a modify must update in place — a delete/replace here would silently
    # destroy the instance (Phase 8 / N-08).
    violation = plan_guard.check_plan_actions("modify", plan["diff"])
    if violation:
        log.error("cloudops.plan_guard_blocked", run_id=run_id, action="modify", summary=plan["summary"])
        await emitter.step(9, "Safety guard · modify plan would replace/destroy — blocked")
        await emitter.error(violation, code="plan_guard", retriable=False)
        return await _say(violation)

    await timing.start_step(run_id, "policy_evaluation")
    policy_checks = template.policy_fn(validated, runner.planned_resources())  # U1: over the real plan
    await timing.end_step(run_id, "policy_evaluation", status="done")

    plan_json = {"summary": plan["summary"], "diff": plan["diff"],
                 "workspace": res["workspace"] or template.workspace,
                 "state_workspace": res.get("state_workspace"),
                 "policy_checks": policy_checks, "mode": "apply"}
    cc = classify(json.dumps({"modify": res["name"], "changes": change_desc}))
    await emitter.confidentiality(cc.level, cc.score)
    reasoning = [
        {"title": "Day-2 modification", "conf": "",
         "body": f"{change_text.capitalize()} on {res['name']} (resource {res.get('provider_id')})."},
        {"title": "Plan", "conf": "",
         "body": f"+{plan['summary']['add']} ~{plan['summary']['change']} -{plan['summary']['destroy']}"},
    ]
    await emitter.analysis(summary=f"Planned a day-2 change on {res['name']}; awaiting approval.", cards=reasoning)
    interrupt_payload = {"kind": "approval", "runId": state["run_id"], "workflow": template.key, "plan": plan_json,
                         "diff": plan["diff"], "policyChecks": policy_checks, "mode": "apply",
                         "cloud": res["cloud"], "resource": res["resource_type"]}
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)
    return {"workflow": template.key, "workflow_version": template.version, "cloud": res["cloud"],
            "resource": res["resource_type"], "state_workspace": res.get("state_workspace"),
            "parsed_inputs": validated, "plan_json": plan_json,
            "diff": plan["diff"], "policy_checks": policy_checks, "execution_mode": "apply",
            "needs_change": True, "approval_status": "pending", "reasoning_cards": reasoning,
            "interrupt_payload": interrupt_payload, "confidentiality": {"level": cc.level, "score": cc.score},
            "answer": f"Planned this change on **{res['name']}**: {change_text} "
                      f"(+{plan['summary']['add']} ~{plan['summary']['change']}). "
                      "Review and approve to apply."}


async def cloudops_execute(state: AgentState, config) -> dict:
    """Apply/destroy the approved plan (post-interrupt). Idempotent + streamed to console."""
    emitter = emitter_of(config)
    settings = get_settings()
    if state.get("approval_status") != "approved":
        return {}
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))
    # Use the cloud/resource resolved + persisted by cloudops_plan (never re-default to AWS).
    template = templates.select(state.get("cloud"), state.get("resource"))
    mode = state.get("execution_mode", "apply")
    if template is None:
        await emitter.error("No approved module for the resolved cloud/resource; refusing to execute.",
                            code="template_error")
        return {"outcome": {"status": f"{mode}_failed", "error": "template not found for resolved cloud/resource"}}
    workspace = template.workspace
    # Execute in the SAME per-resource state workspace the plan was made in (Phase 8 / N-08),
    # and with the SAME run_id so the saved plan-file path matches the one plan wrote (A3).
    runner = TerraformRunner(workspace, settings, state_workspace=state.get("state_workspace"),
                             run_id=state.get("run_id"))

    idem_key = idempotency.make_key("tf-exec", state["run_id"], mode)
    if not await idempotency.claim(idem_key):
        # A1: a concurrent apply already holds the claim. NEVER fall through to a second
        # apply on the same state. If it already finished, return its stored result; if it's
        # still in flight, WAIT for the result up to a deadline, then ABORT (409-style) if it
        # still hasn't landed — abort, never execute.
        done = await idempotency.get_result(idem_key) or await idempotency.wait_for_result(idem_key)
        if done:
            return {"outcome": done["result"], "tool_results": [done["result"]]}
        log.warning("cloudops.execute_already_in_flight", run_id=state["run_id"], mode=mode)
        return {"outcome": {"status": f"{mode}_aborted",
                            "error": "This change is already being applied by another request; "
                                     "aborting to avoid a duplicate apply."},
                "answer": "⚠️ This change is already being applied — I stopped here so nothing "
                          "runs twice. Refresh to see the result of the in-flight apply."}

    plan = state.get("plan_json", {}).get("summary", {})
    n = plan.get("destroy", 0) if mode == "destroy" else plan.get("add", 0) + plan.get("change", 0)
    await emitter.step(5, f"{'Destroying' if mode == 'destroy' else 'Applying'} {n} resources…")

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    async def _cg(coro) -> None:
        # Context-graph writes must never fail a successful infra change.
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.cg_write_failed", error=str(e))

    await _cg(cg.add_step(order=3, name=f"terraform_{mode}", agent="cloudops", tool="terraform",
                          status="running", human_vs_auto="auto"))
    try:
        result = await (runner.destroy(state.get("parsed_inputs", {}), on_line) if mode == "destroy"
                        else runner.apply(on_line))
    except TerraformError as e:
        await idempotency.release(idem_key)
        # Classify + explain the failure in the conversation (Phase 7 / BUG-05); raw trace
        # stays in the Logs tab. Also report what (if anything) is left in Terraform state so
        # the user knows whether partial resources remain.
        failure = provider_errors.classify_provider_error(str(e))
        friendly = provider_errors.failure_message(failure, str(e), mode=mode)
        try:
            leftover = await runner.state_list()
        except Exception:  # noqa: BLE001 - the state report is best-effort
            leftover = None
        if leftover is not None:
            friendly += ("\n\n**State check:** "
                         + (f"{len(leftover)} resource(s) remain in Terraform state "
                            f"(`{'`, `'.join(leftover[:6])}`) — a retry will reconcile them, or ask "
                            "me to destroy this workspace to clean up."
                            if leftover else "no partial resources were left behind."))
        # U7: one-click retry-with-fix (a genuine new turn through the whole gated flow).
        retry = provider_errors.suggest_retry(
            failure, state.get("message", ""), cloud=state.get("cloud"),
            current_region=(state.get("parsed_inputs") or {}).get("region")
            or (state.get("parsed_inputs") or {}).get("location"))
        await emitter.error(f"terraform {mode} failed: {e}", code="terraform_error",
                            retriable=True, retry=retry)
        await emitter.token(friendly)
        cc = classify(friendly)
        await emitter.confidentiality(cc.level, cc.score)
        await _cg(cg.update_step(order=3, status="failed", error=str(e)))
        return {"outcome": {"status": f"{mode}_failed", "error": str(e)[:500],
                            "failure": failure.__dict__ if failure else None,
                            "retry": retry},
                "answer": friendly,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    await idempotency.store_result(idem_key, result)
    await _cg(cg.update_step(order=3, status="done", result={"mode": mode}))
    # D2: the apply already mutated real infrastructure — that cannot be rolled back. So the
    # inventory row and the run outcome are written in ONE transaction, and the outcome carries a
    # self-contained recovery payload (`_inventory`). Result: normally both land together; and
    # even if this write is interrupted, the outcome returned below is persisted by the outer
    # driver WITH the payload, so the orphan sweeper can rebuild a missing inventory row from the
    # run alone. A real applied resource is never invisible.
    if mode == "destroy":
        outcome = {"status": "destroyed", **result}
        name = inventory.name_from_inputs(state.get("parsed_inputs") or {}, template.resource)
        try:
            async with session_scope() as s:
                await inventory.mark_destroyed_txn(s, state["org_id"], template.workspace, name)
                run = await s.get(Run, uuid.UUID(state["run_id"]))
                if run:
                    run.outcome = outcome
        except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a real destroy
            log.warning("cloudops.inventory_failed", error=str(e))
        await inventory.mark_destroyed_graph_only(name, org_id=state["org_id"])
    else:
        payload = inventory.inventory_payload(state, template, result.get("outputs", {}))
        outcome = {"status": "applied", **result, "_inventory": payload}
        try:
            async with session_scope() as s:
                await inventory.upsert_resource(s, state["org_id"], payload)
                run = await s.get(Run, uuid.UUID(state["run_id"]))
                if run:
                    run.outcome = outcome
        except Exception as e:  # noqa: BLE001 - inventory bookkeeping must never fail a real apply
            log.warning("cloudops.inventory_failed", error=str(e))
        await inventory.record_graph(state, template, result.get("outputs", {}))
    return {"outcome": outcome, "tool_results": [result]}
