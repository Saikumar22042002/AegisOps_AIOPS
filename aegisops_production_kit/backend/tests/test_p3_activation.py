"""Prompt 3 — harness/engine activation pins (2026-08-17).

The durable engine gains real-infrastructure semantics: retain-on-failure (mandate 20 —
never auto-destroy applied infra because a later step failed), step-boundary cancellation
(mandate 22), and the terraform StepExecutor draft adaptation from the exec_loop goal DAG.
Also pins already-satisfied honesty in the loop's step reports.
"""

from __future__ import annotations

import uuid

import pytest

from app.engine.dag import compile_workflow


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _draft():
    return [
        {"id": "vpc", "kind": "module", "name": "net", "template_key": "aws.vpc",
         "params": {}, "output_id": "vpc"},
        {"id": "ec2", "kind": "module", "name": "vm", "template_key": "aws.ec2",
         "params": {}, "depends_on": ["vpc"], "output_id": "ec2"},
    ]


async def _mk_run(org_id: str) -> str:
    from app.db.models import Run
    from app.db.session import session_scope
    async with session_scope() as s:
        run = Run(org_id=uuid.UUID(org_id), status="running", mode="apply", domain="cloudops")
        s.add(run)
        await s.flush()
        return str(run.id)


# ── pure: exec_loop → engine draft adaptation ──────────────────────────────────────────────

def test_goal_dag_draft_adaptation_shape():
    """The DEP-drafted goal DAG maps onto engine draft ids == template keys, so
    resolve_wires' observation keys line up without translation."""
    dag = [{"template_key": "aws.vpc", "inputs": {"name": "net"}, "provides": "vpc"},
           {"template_key": "aws.ec2", "inputs": {"name": "vm"},
            "wires": {"subnet_id": "public_subnet_ids[0]"}, "depends_on": "aws.vpc"}]
    draft = [{"id": s["template_key"], "kind": "module",
              "name": s["inputs"].get("name", s["template_key"]),
              "template_key": s["template_key"], "params": dict(s),
              "depends_on": [s["depends_on"]] if s.get("depends_on") else []}
             for s in dag]
    wf = compile_workflow(draft, run_id="r-adapt")
    assert wf.waves == (("aws.vpc",), ("aws.ec2",))
    assert wf.step("aws.ec2").params["wires"] == {"subnet_id": "public_subnet_ids[0]"}


# ── live-DB: retain-on-failure + cancellation ──────────────────────────────────────────────

@pytest.mark.usefixtures("live_db", "live_redis")
async def test_retain_on_failure_keeps_applied_steps_and_never_compensates(throwaway_org):
    from app.engine.engine import StepOutcome, execute_workflow
    from app.settings import get_settings

    run_id = await _mk_run(throwaway_org)
    wf = compile_workflow(_draft(), run_id=run_id)
    compensations: list[str] = []

    async def executor(step, outputs):
        if step.id == "vpc":
            return StepOutcome(ok=True, result={"status": "applied", "outputs": {"vpc_id": "vpc-1"}})
        return StepOutcome(ok=False, error="RunInstances blocked")

    async def compensator(step):  # must NEVER run under retain policy
        compensations.append(step.id)
        return True

    res = await execute_workflow(get_settings(), run_id, wf, executor=executor,
                                 compensator=compensator, org_id=throwaway_org,
                                 on_failure="retain")
    assert res.status == "failed" and res.failed_step == "ec2"
    assert res.completed == ["vpc"]          # applied work retained…
    assert compensations == []               # …and NEVER auto-destroyed (mandate 20)


@pytest.mark.usefixtures("live_db", "live_redis")
async def test_cancellation_stops_at_step_boundary(throwaway_org):
    from app.engine.engine import StepOutcome, execute_workflow
    from app.settings import get_settings

    run_id = await _mk_run(throwaway_org)
    wf = compile_workflow(_draft(), run_id=run_id)
    cancelled = {"flag": False}

    async def executor(step, outputs):
        cancelled["flag"] = True             # cancel arrives while step 1 runs
        return StepOutcome(ok=True, result={"status": "applied"})

    async def should_cancel():
        return cancelled["flag"]

    res = await execute_workflow(get_settings(), run_id, wf, executor=executor,
                                 compensator=None, org_id=throwaway_org,
                                 on_failure="retain", should_cancel=should_cancel)
    assert res.status == "cancelled"
    assert res.completed == ["vpc"]          # the running step finished cleanly
    assert res.failed_step == "ec2"          # …and the next never started


@pytest.mark.usefixtures("live_db", "live_redis")
async def test_retain_failure_then_recover_resumes_without_reapply(throwaway_org):
    """Mandate J/21: fail step 2 (retain), then 'restart' — the recovery run must skip the
    completed step (claim recovery) and only run the failed one."""
    from app.engine.engine import StepOutcome, execute_workflow
    from app.settings import get_settings

    run_id = await _mk_run(throwaway_org)
    wf = compile_workflow(_draft(), run_id=run_id)
    calls: dict[str, int] = {}

    def make_executor(ec2_ok: bool):
        async def executor(step, outputs):
            calls[step.id] = calls.get(step.id, 0) + 1
            if step.id == "ec2" and not ec2_ok:
                return StepOutcome(ok=False, error="transient")
            return StepOutcome(ok=True, result={"status": "applied", "id": step.id})
        return executor

    first = await execute_workflow(get_settings(), run_id, wf,
                                   executor=make_executor(False), compensator=None,
                                   org_id=throwaway_org, on_failure="retain")
    assert first.status == "failed" and calls == {"vpc": 1, "ec2": 1}

    second = await execute_workflow(get_settings(), run_id, wf,
                                    executor=make_executor(True), compensator=None,
                                    org_id=throwaway_org, on_failure="retain")
    assert second.status == "completed"
    assert calls["vpc"] == 1                 # completed work NEVER re-applied
    assert "vpc" in second.recovered and calls["ec2"] == 2


# ── pure: already-satisfied surfaces honestly in the loop ─────────────────────────────────

async def test_execute_goal_dag_reports_already_satisfied(monkeypatch):
    from app.agents import exec_loop

    async def fake_step(state, step, i, config, observations):
        return {"status": "already_satisfied", "template": step["template_key"],
                "name": "net", "inputs": {}, "outputs": {"vpc_id": "vpc-1"},
                "policy_checks": []}

    class _Emitter:
        async def token(self, *a, **k): ...
        async def confidentiality(self, *a, **k): ...
        async def step(self, *a, **k): ...

    monkeypatch.setattr(exec_loop, "execute_governed_step", fake_step)
    monkeypatch.setattr(exec_loop, "emitter_of", lambda config: _Emitter())
    monkeypatch.setattr(exec_loop, "get_settings",
                        lambda: type("S", (), {"aegisops_durable_engine": "off"})())
    out = await exec_loop.execute_goal_dag(
        {"goal_dag": [{"template_key": "aws.vpc", "inputs": {"name": "net"}}],
         "run_id": None, "org_id": "o"}, config=None)
    assert out["outcome"]["status"] == "applied"
    assert out["outcome"]["steps"][0]["status"] == "already_satisfied"
    assert "already satisfied" in out["answer"]
