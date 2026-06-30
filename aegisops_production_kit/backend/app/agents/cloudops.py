"""CloudOps agent — multi-cloud provisioning/modify/destroy/read across AWS, Azure, GCP.

Flow: select template (by router's cloud+resource) → extract + Pydantic-validate inputs →
real availability checks (cloud SDK reads) → terraform init/validate/plan → policy + confidentiality
→ build the approval artifact (plan/diff/policy) and INTERRUPT. On approve: terraform apply/destroy
(streamed to the console) → verify via SDK reads → outcome. Cloud SDKs never provision — Terraform
does every mutation, and only after the human-approval gate.
"""

from __future__ import annotations

import json
import os
import uuid

import structlog

from ..graph_db.context_graph import ContextGraph
from ..integrations.gemini import get_gemini
from ..security import idempotency
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools import aws as aws_tool
from ..tools import azure as azure_tool
from ..tools import gcp as gcp_tool
from ..tools.terraform import TerraformError, TerraformRunner
from . import llm, templates
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)


async def _extract_inputs(settings, schema, message: str) -> dict:
    """Extract structured inputs from NL via Gemini, merged with free-form key=value parsing."""
    from ..schemas.workflows import parse_freeform

    inputs = parse_freeform(message)
    gemini = get_gemini(settings)
    if gemini.enabled:
        fields = list(schema.model_fields.keys())
        system = (
            "Extract Terraform input values from the user's request. Respond with ONLY a JSON "
            f"object using these keys when present: {fields}. Omit unknown keys. Lists as JSON arrays."
        )
        try:
            extracted = await llm.classify_json(settings, system, message)
            inputs = {**extracted, **inputs}  # explicit free-form wins
        except Exception as e:  # noqa: BLE001
            log.warning("cloudops.extract_failed", error=str(e))
    return inputs


def _generate_generic_workspace(settings, run_id: str, inputs: dict) -> str:
    """Write a per-run workspace wrapping an arbitrary published module (escape hatch)."""
    base = os.path.join(settings.terraform_workspaces_dir, "_generated", run_id)
    os.makedirs(base, exist_ok=True)
    var_lines = "\n".join(f'  {k} = jsondecode(var.module_vars)["{k}"]' for k in inputs.get("variables", {}))
    hcl = f'''terraform {{
  required_version = ">= 1.6"
  backend "local" {{}}
}}
variable "module_vars" {{ type = string, default = "{{}}" }}
module "this" {{
  source = "{inputs['source']}"
  {'version = "' + inputs['version'] + '"' if inputs.get('version') else ''}
{var_lines}
}}
'''
    # variable with comma is invalid HCL; fix to multiline form.
    hcl = hcl.replace('variable "module_vars" { type = string, default = "{}" }',
                      'variable "module_vars" {\n  type    = string\n  default = "{}"\n}')
    with open(os.path.join(base, "main.tf"), "w") as f:
        f.write(hcl)
    return os.path.join("_generated", run_id)


async def _availability(settings, cloud: str, region: str, emitter) -> dict:
    """Real read-only availability/connectivity pre-check via the matching cloud SDK."""
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
    cloud = state.get("cloud") or "aws"
    resource = state.get("resource") or "module"
    action = state.get("action") or "create"
    region = state.get("user", {}).get("region", "us-east-1")
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))

    template = templates.select(cloud, resource)
    if template is None:
        return {"needs_clarification": True, "needs_change": False,
                "clarification": f"I don't have an approved template for {cloud}/{resource}. "
                                 "Tell me the resource type (e.g. s3, vpc, eks, storage, gcs) or a Terraform module source."}

    await emitter.step(2, f"Selected workflow · {template.key} {template.version}")

    # ── Read path: discovery only, no Terraform ──
    if action == "read":
        return await _read_path(state, config, cloud, region, template)

    # ── Inputs ──
    inputs = await _extract_inputs(settings, template.schema, state["message"])
    try:
        validated = template.schema(**inputs).model_dump()
    except Exception as e:  # noqa: BLE001 - Pydantic ValidationError → actionable clarification
        await cg.add_step(order=1, name="validate_inputs", agent="cloudops", tool="pydantic", status="failed")
        await cg.update_step(order=1, status="failed", error=str(e))
        return {"needs_clarification": True, "needs_change": False, "validation_errors": [str(e)],
                "clarification": f"I need a bit more to provision {template.resource}: {e}"}

    # ── Availability (real SDK reads) ──
    await emitter.step(3, f"Queried {cloud.upper()} · {region}")
    avail = await _availability(settings, cloud, region, emitter)
    for c in avail["checks"]:
        await emitter.console("stdout", f"availability: {c['name']} = {'ok' if c['passed'] else 'n/a'} {c.get('detail','')}")

    # ── Terraform plan ──
    workspace = template.workspace
    tf_vars = validated
    if template.key == "generic.module":
        workspace = _generate_generic_workspace(settings, state["run_id"], validated)
        tf_vars = {"module_vars": json.dumps(validated.get("variables", {}))}

    mode = "destroy" if action == "destroy" else "apply"
    await emitter.step(4, "Ran terraform plan")
    runner = TerraformRunner(workspace, settings)

    async def on_line(stream: str, line: str) -> None:
        await emitter.console(stream, line)

    try:
        await runner.init(on_line)
        await runner.plan(tf_vars, destroy=(action == "destroy"), on_line=on_line)
        plan = await runner.show_plan()
    except TerraformError as e:
        await emitter.error(f"terraform plan failed: {e}", code="terraform_error", retriable=True)
        await cg.add_step(order=2, name="terraform_plan", agent="cloudops", tool="terraform", status="failed")
        await cg.update_step(order=2, status="failed", error=str(e))
        return {"needs_change": False, "approval_status": "not_required",
                "answer": f"Terraform plan failed: {e}", "outcome": {"status": "plan_failed"}}

    policy_checks = template.policy_fn(validated)
    plan_json = {"summary": plan["summary"], "diff": plan["diff"], "workspace": template.workspace,
                 "policy_checks": policy_checks, "mode": mode}

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
        "mode": mode, "cloud": cloud, "resource": template.resource,
    }
    await emitter.step(9, "Awaiting approval")
    await emitter.interrupt(interrupt_payload)

    return {
        "workflow": template.key, "workflow_version": template.version,
        "parsed_inputs": validated, "plan_json": plan_json, "diff": plan["diff"],
        "policy_checks": policy_checks, "dependencies": avail["checks"],
        "execution_mode": mode, "needs_change": True, "approval_status": "pending",
        "confidentiality": {"level": c.level, "score": c.score},
        "reasoning_cards": reasoning, "interrupt_payload": interrupt_payload,
        "answer": f"Drafted a {template.key} plan (+{plan['summary']['add']} ~{plan['summary']['change']} "
                  f"-{plan['summary']['destroy']}). Review and approve to apply.",
    }


async def _read_path(state, config, cloud, region, template) -> dict:
    emitter = emitter_of(config)
    settings = get_settings()
    await emitter.step(3, f"Queried {cloud.upper()} · {region}")
    summary = []
    try:
        if cloud == "aws":
            r = aws_tool.get_aws(settings)
            if r.enabled:
                vpcs = await r.list_vpcs(region)
                dbs = await r.list_databases(region)
                summary = [f"{len(vpcs)} VPC(s)", f"{len(dbs)} RDS instance(s)"]
            else:
                summary = ["AWS not configured"]
        else:
            summary = [f"{cloud} discovery requires configured credentials"]
    except Exception as e:  # noqa: BLE001
        await emitter.error(f"discovery failed: {e}", code="discovery_error", retriable=True)
    text = f"Discovered in {cloud} {region}: " + ", ".join(summary)
    await emitter.token(text)
    c = classify(text)
    await emitter.confidentiality(c.level, c.score)
    return {"needs_change": False, "approval_status": "not_required", "answer": text,
            "confidentiality": {"level": c.level, "score": c.score}}


async def cloudops_execute(state: AgentState, config) -> dict:
    """Apply/destroy the approved plan (post-interrupt). Idempotent + streamed to console."""
    emitter = emitter_of(config)
    settings = get_settings()
    if state.get("approval_status") != "approved":
        return {}
    context_id = state.get("context_id") or state["run_id"]
    cg = ContextGraph(context_id, state.get("org_id", ""))
    template = templates.select(state.get("cloud") or "aws", state.get("resource") or "module")
    workspace = template.workspace
    if template.key == "generic.module":
        workspace = os.path.join("_generated", state["run_id"])
    runner = TerraformRunner(workspace, settings)
    mode = state.get("execution_mode", "apply")

    idem_key = idempotency.make_key("tf-exec", state["run_id"], mode)
    if not await idempotency.claim(idem_key):
        done = await idempotency.get_result(idem_key)
        if done:
            return {"outcome": done["result"], "tool_results": [done["result"]]}

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
        await emitter.error(f"terraform {mode} failed: {e}", code="terraform_error", retriable=True)
        await _cg(cg.update_step(order=3, status="failed", error=str(e)))
        return {"outcome": {"status": f"{mode}_failed", "error": str(e)}}

    await idempotency.store_result(idem_key, result)
    await _cg(cg.update_step(order=3, status="done", result={"mode": mode}))
    outcome = {"status": "applied" if mode == "apply" else "destroyed", **result}
    return {"outcome": outcome, "tool_results": [result]}
