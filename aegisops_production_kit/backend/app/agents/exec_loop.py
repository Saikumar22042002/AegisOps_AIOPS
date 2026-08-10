"""Governed Executive Loop (U6).

Multi-step provisioning as a **goal DAG**: each node is an approved module + params (produced
deterministically by the DEP resolver today; an LLM planner can draft the same structure when
one is configured). The human approves the WHOLE DAG once; deterministic code then executes
each step via `execute_governed_step` — plan → plan-guard → policy → apply — feeding each
step's real outputs into the next step's wires. Anything that would change the approved DAG
(a replan after a failed step) is a **deviation** and triggers a fresh approval interrupt.

Hard bounds (never exceeded, breach halts honestly): MAX_STEPS per DAG, MAX_REPLANS per step.
Steps are idempotent across interrupt-replays (LangGraph re-runs a node body on resume) via the
A1 claim/stored-result machinery — an already-applied step returns its stored result and is
NEVER re-applied. Partial failure is reported honestly ("steps 1–2 applied, step 3 failed: …").

Gated by `AEGISOPS_EXEC_LOOP` (default off → the DEP dag branch proposes the plan as text).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import structlog
from langgraph.types import interrupt

from ..security import idempotency
from ..security.confidentiality import classify
from ..settings import get_settings
from ..tools.terraform import TerraformError, TerraformRunner, state_slug
from . import inventory, plan_guard, templates, timing
from .runtime import emitter_of
from .state import AgentState

log = structlog.get_logger(__name__)

MAX_STEPS = 5           # hard ceiling on DAG size
MAX_REPLANS_PER_STEP = 1

# Test/extension seam: the re-approval request. In the graph this is LangGraph's interrupt();
# tests monkeypatch it to capture the payload without a running graph.
_request_reapproval: Callable[[dict], Any] = interrupt

# Replan hook: (step, observation) -> revised step | None. The default never invents a fix —
# no replan means an honest halt. U7 (retry-with-fix) and an LLM planner plug in here; ANY
# revision deviates from the approved DAG and must be re-approved.
_replan_step: Callable[[dict, dict], dict | None] = lambda step, observation: None

_WIRE_INDEX = re.compile(r"^(?P<key>[\w-]+)\[(?P<idx>\d+)\]$")


def resolve_wires(step: dict, observations: dict[str, dict]) -> dict:
    """Fill the step's wired fields from PRIOR steps' real outputs/inputs (pure).

    Wire exprs: "<output_key>", "<output_key>[i]", or "input:<field>" (the parent's input).
    Raises KeyError with a plain message when a wire can't be satisfied — never guesses.
    """
    inputs = dict(step.get("inputs") or {})
    parent_key = step.get("depends_on")
    if not step.get("wires"):
        return inputs
    if parent_key not in observations:
        raise KeyError(f"step depends on '{parent_key}' which has not produced outputs yet")
    parent = observations[parent_key]
    for field, expr in (step.get("wires") or {}).items():
        if expr.startswith("input:"):
            src = (parent.get("inputs") or {}).get(expr[6:])
        else:
            m = _WIRE_INDEX.match(expr)
            outputs = parent.get("outputs") or {}
            if m:
                seq = outputs.get(m.group("key"))
                src = seq[int(m.group("idx"))] if isinstance(seq, list) and len(seq) > int(m.group("idx")) else None
            else:
                src = outputs.get(expr)
        if src in (None, "", []):
            raise KeyError(f"wire '{field} ← {expr}' has no value in step '{parent_key}' outputs")
        inputs[field] = src
    return inputs


def validate_dag(dag: list[dict]) -> str | None:
    """Bounds + governance over the DAG shape (pure). Returns a refusal message or None.

    Every node must reference an APPROVED template (there is no runtime-HCL escape hatch) and
    the DAG must fit the hard step ceiling.
    """
    if not dag:
        return "The goal DAG is empty — nothing to execute."
    if len(dag) > MAX_STEPS:
        return (f"The plan needs {len(dag)} steps, above the governed ceiling of {MAX_STEPS}. "
                "Split the request — I won't run an unbounded plan.")
    for step in dag:
        key = step.get("template_key", "")
        if templates.by_key(key) is None:
            return (f"Step '{key}' is not an approved module — the loop only executes the "
                    "approved catalog, never generated infrastructure code.")
    return None


async def plan_goal_dag(state: AgentState, config, dag: list[dict]) -> dict:
    """PLAN phase: validate the DAG, terraform-plan every step whose inputs are already
    concrete, and raise ONE approval interrupt whose card lists every step.

    A step wired to a parent's outputs cannot honestly be planned before that parent exists —
    its card entry states that, and it is planned (plan-guarded + policy-checked) at execute
    time. What the human approves for it: the approved module + declared inputs + wires."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")

    refusal = validate_dag(dag)
    if refusal:
        await emitter.token(refusal)
        cc = classify(refusal)
        await emitter.confidentiality(cc.level, cc.score)
        return {"needs_change": False, "approval_status": "not_required", "answer": refusal,
                "confidentiality": {"level": cc.level, "score": cc.score}}

    steps_card: list[dict] = []
    first_diff: list[dict] = []
    for i, step in enumerate(dag):
        template = templates.by_key(step["template_key"])
        entry: dict[str, Any] = {"order": i + 1, "template": template.key,
                                 "name": _step_name(step), "inputs": step.get("inputs") or {},
                                 "wires": step.get("wires") or {}}
        if step.get("wires"):
            parent_no = next((j + 1 for j, s in enumerate(dag)
                              if s["template_key"] == step.get("depends_on")), 0)
            entry["plan"] = (f"planned at execute time — inputs wired to step {parent_no}'s "
                             "real outputs")
            entry["policy_checks"] = [{"name": "Policy over the real plan", "passed": None,
                                       "evaluated": False,
                                       "detail": "evaluated when this step plans (after its parent applies)"}]
        else:
            validated = template.schema(**(step.get("inputs") or {})).model_dump()
            step["inputs"] = validated
            runner = TerraformRunner(template.workspace, settings,
                                     state_workspace=state_slug(_step_name(step)),
                                     run_id=f"{run_id}-s{i}")
            await emitter.step(4, f"Planned step {i + 1}/{len(dag)} · {template.key}")
            await runner.init()
            plan = await runner.plan(validated)
            violation = plan_guard.check_plan_actions("create", plan["diff"])
            if violation:
                await emitter.error(violation, code="plan_guard", retriable=False)
                return {"needs_change": False, "approval_status": "not_required",
                        "answer": violation, "outcome": {"status": "blocked", "error": violation}}
            entry["plan"] = plan["summary"]
            entry["policy_checks"] = template.policy_fn(validated, runner.planned_resources())
            if not first_diff:
                first_diff = plan["diff"]
        steps_card.append(entry)

    payload = {"kind": "approval", "runId": run_id, "workflow": "governed-exec-loop",
               "mode": "apply",
               "plan": {"steps": steps_card,
                        "summary": {"add": sum((s.get("plan") or {}).get("add", 0)
                                               for s in steps_card if isinstance(s.get("plan"), dict)),
                                    "change": 0, "destroy": 0}},
               "policyChecks": [c for s in steps_card for c in s.get("policy_checks", [])]}
    # P0.5 (D9/F-9): governance posture on the whole-DAG card too — additive field only.
    from ..security.governance_stamp import stamped
    payload = stamped(payload)
    await emitter.step(9, "Awaiting approval · whole goal DAG, one decision")
    await emitter.interrupt(payload)
    answer = (f"Drafted a governed {len(dag)}-step plan (" +
              " → ".join(f"{s['order']}) {s['template']} “{s['name']}”" for s in steps_card) +
              "). One approval covers the whole DAG; each step still plans, plan-guards and "
              "policy-checks before it applies, and any deviation re-asks you.")
    cc = classify(answer)
    await emitter.confidentiality(cc.level, cc.score)
    return {"workflow": "governed-exec-loop", "workflow_version": "v1",
            "needs_change": True, "approval_status": "pending", "execution_mode": "apply",
            "goal_dag": dag, "interrupt_payload": payload, "answer": answer,
            "plan_json": {"summary": payload["plan"]["summary"], "diff": first_diff,
                          "steps": steps_card, "policy_checks": payload["policyChecks"],
                          "mode": "apply", "workspace": "governed-exec-loop"},
            "confidentiality": {"level": cc.level, "score": cc.score}}


def _step_name(step: dict) -> str:
    inp = step.get("inputs") or {}
    return str(inp.get("name") or inp.get("cluster_name") or inp.get("account_name")
               or step.get("template_key", "step"))


async def _record_step_bookkeeping(step_state: dict, template, outputs: dict) -> None:
    """Inventory row + graph/world-model mirror for one applied step (best-effort — the D2
    same-txn guarantee for loop steps rides on the outcome's per-step reports; bookkeeping
    never fails a real apply)."""
    payload = inventory.inventory_payload(step_state, template, outputs)
    try:
        from ..db.session import session_scope
        async with session_scope() as s:
            await inventory.upsert_resource(s, step_state["org_id"], payload)
    except Exception as e:  # noqa: BLE001
        log.warning("exec_loop.inventory_failed", error=str(e))
    await inventory.record_graph(step_state, template, outputs)


async def execute_governed_step(state: AgentState, step: dict, index: int, config,
                                observations: dict[str, dict]) -> dict:
    """Deterministic single-step execution: resolve wires → validate → plan → plan-guard →
    policy → apply → record. Idempotent across interrupt-replays via the A1 claim/result store
    (an already-applied step returns its stored result, never re-applies)."""
    emitter = emitter_of(config)
    settings = get_settings()
    run_id = state.get("run_id")
    template = templates.by_key(step["template_key"])
    step_label = f"loop_step_{index + 1}_{template.key}"

    idem_key = idempotency.make_key("loop-step", run_id, index)
    if not await idempotency.claim(idem_key):
        done = await idempotency.get_result(idem_key) or await idempotency.wait_for_result(idem_key)
        if done:
            return done["result"]
        return {"status": "aborted",
                "error": "this step is already being applied by another request — aborted, never doubled"}

    await timing.start_step(run_id, step_label, tool="terraform")
    try:
        inputs = resolve_wires(step, observations)
        validated = template.schema(**inputs).model_dump()

        async def on_line(stream: str, line: str) -> None:
            await emitter.console(stream, line)

        runner = TerraformRunner(template.workspace, settings,
                                 state_workspace=state_slug(_step_name({"inputs": validated,
                                                                        "template_key": template.key})),
                                 run_id=f"{run_id}-s{index}")
        await emitter.step(5, f"Step {index + 1} · {template.key} · plan")
        await runner.init(on_line)
        plan = await runner.plan(validated, on_line=on_line)

        violation = plan_guard.check_plan_actions("create", plan["diff"])
        if violation:
            raise TerraformError(violation)
        checks = template.policy_fn(validated, runner.planned_resources())
        failed = [c for c in checks if c.get("evaluated") is not False and c.get("passed") is False]
        if failed:
            names = ", ".join(c["name"] for c in failed)
            raise TerraformError(f"policy check(s) failed on the real plan: {names} — not applying")

        await emitter.step(5, f"Step {index + 1} · {template.key} · apply")
        result = await runner.apply(on_line)
        outputs = result.get("outputs", {})

        # Inventory + world model, same facts as a single-step apply (D2/D3 ingestion).
        step_state = {**state, "parsed_inputs": validated,
                      "state_workspace": state_slug(_step_name({"inputs": validated,
                                                                "template_key": template.key}))}
        await _record_step_bookkeeping(step_state, template, outputs)

        observation = {"status": "applied", "template": template.key,
                       "name": _step_name(step), "inputs": validated, "outputs": outputs,
                       "policy_checks": checks}
        await timing.end_step(run_id, step_label, status="done", result={"applied": True})
        await idempotency.store_result(idem_key, observation)
        return observation
    except Exception as e:  # noqa: BLE001 — a failed step is an honest observation, not a crash
        await idempotency.release(idem_key)
        await timing.end_step(run_id, step_label, status="failed", error=str(e))
        await emitter.error(f"step {index + 1} ({template.key}) failed: {e}",
                            code="loop_step_failed", retriable=True)
        return {"status": "failed", "template": template.key, "name": _step_name(step),
                "error": str(e)[:500]}


async def execute_goal_dag(state: AgentState, config) -> dict:
    """EXECUTE phase (post-approval): run the approved DAG in order, wiring real outputs
    forward. A replanned (revised) step is a DEVIATION → fresh approval interrupt. Bounds and
    failures halt honestly with a partial report — later steps are never attempted blind."""
    emitter = emitter_of(config)
    dag: list[dict] = state.get("goal_dag") or []
    observations: dict[str, dict] = {}
    step_reports: list[dict] = []

    for i, step in enumerate(dag):
        # PR-3c: cancel is honored at the STEP BOUNDARY — halt-after-current-step, NEVER
        # mid-apply. Steps already applied stay applied; the next never starts.
        if i > 0 and await _cancel_requested(state.get("run_id")):
            return _partial_outcome(step_reports, dag, i - 1,
                                    {"error": "cancelled by user"}, halted="cancelled")
        replans = 0
        current = step
        while True:
            obs = await execute_governed_step(state, current, i, config, observations)
            if obs.get("status") == "applied":
                observations[current["template_key"]] = obs
                step_reports.append({"order": i + 1, "template": obs["template"],
                                     "name": obs["name"], "status": "applied"})
                break

            step_reports.append({"order": i + 1, "template": current.get("template_key"),
                                 "name": _step_name(current), "status": "failed",
                                 "error": obs.get("error")})
            revised = _replan_step(current, obs)
            if revised is None or replans >= MAX_REPLANS_PER_STEP:
                return _partial_outcome(step_reports, dag, i, obs, halted="failure"
                                        if revised is None else "replan bound reached")
            replans += 1
            # DEVIATION: the revised step differs from what the human approved → re-ask.
            decision = _request_reapproval({
                "kind": "approval", "runId": state.get("run_id"),
                "workflow": "governed-exec-loop", "mode": "apply", "reason": "deviation",
                "plan": {"deviation": {"step": i + 1, "was": current.get("inputs"),
                                       "now": revised.get("inputs"),
                                       "template": revised.get("template_key")},
                         "steps": []},
                "policyChecks": []})
            status = decision.get("decision") if isinstance(decision, dict) else str(decision)
            if status != "approved":
                return _partial_outcome(step_reports, dag, i, obs, halted="deviation rejected")
            step_reports[-1]["status"] = "replanned"
            current = revised

    answer = ("Governed plan complete: " +
              "; ".join(f"step {r['order']} ({r['template']} “{r['name']}”) applied"
                        for r in step_reports if r["status"] == "applied") + ".")
    await emitter.token(answer)
    cc = classify(answer)
    await emitter.confidentiality(cc.level, cc.score)
    return {"outcome": {"status": "applied", "steps": step_reports,
                        "outputs": {k: v.get("outputs", {}) for k, v in observations.items()}},
            "answer": answer, "tool_results": [step_reports],
            "confidentiality": {"level": cc.level, "score": cc.score}}


async def _cancel_requested(run_id: str | None) -> bool:
    if not run_id:
        return False
    from .supervisor import is_cancelled
    return await is_cancelled(run_id)


def _partial_outcome(step_reports: list[dict], dag: list[dict], failed_index: int,
                     obs: dict, halted: str) -> dict:
    applied = [r for r in step_reports if r["status"] == "applied"]
    applied_txt = (f"steps {', '.join(str(r['order']) for r in applied)} applied"
                   if applied else "no steps applied")
    if halted == "cancelled":
        # PR-3c honest partial: "steps 1–2 applied, cancelled before step 3."
        next_step = failed_index + 2
        answer = (f"🛑 Cancelled: {applied_txt}"
                  + (f", cancelled before step {next_step}." if next_step <= len(dag)
                     else "; nothing further was attempted."))
        return {"outcome": {"status": "cancelled", "steps": step_reports,
                            "halted": "cancelled"},
                "answer": answer}
    remaining = len(dag) - failed_index - 1
    answer = (f"⚠️ Governed plan halted ({halted}): {applied_txt}; step {failed_index + 1} "
              f"failed: {obs.get('error', 'unknown error')}."
              + (f" {remaining} later step(s) were not attempted." if remaining else ""))
    return {"outcome": {"status": "partial_failure", "steps": step_reports,
                        "halted": halted, "error": obs.get("error")},
            "answer": answer}
