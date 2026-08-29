"""U6 — Governed Executive Loop: one approval for the whole DAG, deterministic per-step
execution with real output wiring, deviation → re-approval, hard bounds, honest partial
failure. Terraform is faked at the runner seam; idempotency/dedup uses live Redis.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents import exec_loop
from app.agents.exec_loop import (MAX_REPLANS_PER_STEP, MAX_STEPS, execute_goal_dag,
                                  resolve_wires, validate_dag)


# ── pure pieces ────────────────────────────────────────────────────────────────────────────

def test_resolve_wires_from_parent_outputs_and_inputs():
    observations = {"aws.vpc": {"inputs": {"name": "web-net"},
                                "outputs": {"vpc_id": "vpc-9",
                                            "public_subnet_ids": ["sub-a", "sub-b"]}}}
    step = {"template_key": "aws.ec2", "depends_on": "aws.vpc",
            "inputs": {"name": "web"},
            "wires": {"subnet_id": "public_subnet_ids[0]", "vpc_ref": "vpc_id",
                      "rg": "input:name"}}
    resolved = resolve_wires(step, observations)
    assert resolved["subnet_id"] == "sub-a"
    assert resolved["vpc_ref"] == "vpc-9"
    assert resolved["rg"] == "web-net"
    assert resolved["name"] == "web"


def test_resolve_wires_missing_output_raises_never_guesses():
    step = {"template_key": "aws.ec2", "depends_on": "aws.vpc",
            "inputs": {}, "wires": {"subnet_id": "public_subnet_ids[0]"}}
    with pytest.raises(KeyError):
        resolve_wires(step, {"aws.vpc": {"outputs": {}}})


def test_validate_dag_bounds_and_governance():
    too_many = [{"template_key": "aws.s3", "inputs": {}} for _ in range(MAX_STEPS + 1)]
    assert "ceiling" in validate_dag(too_many)
    assert "not an approved module" in validate_dag([{"template_key": "evil.hcl", "inputs": {}}])
    assert validate_dag([]) is not None
    assert validate_dag([{"template_key": "aws.vpc", "inputs": {"name": "n"}}]) is None


# ── execute phase with a fake runner ───────────────────────────────────────────────────────

class _FakeRunner:
    """Stands in for TerraformRunner: records applies, serves canned plans/outputs."""

    applies: list[str] = []          # class-level: order across instances
    fail_applies: set[str] = set()   # workspace names whose apply should fail

    def __init__(self, workspace, settings, state_workspace=None, run_id=None):
        self.workspace = workspace
        self.state_workspace = state_workspace
        # A compliant EC2 plan document — the loop runs the REAL policy predicates over these
        # (a bare `after` would honestly fail IMDSv2/encryption and halt the loop).
        self._resources = ([{"type": "aws_instance",
                             "after": {"metadata_options": [{"http_tokens": "required"}],
                                       "root_block_device": [{"encrypted": True}]}}]
                           if workspace == "aws-ec2" else [{"type": "module", "after": {}}])

    async def init(self, on_line=None, force=False):
        return {}

    async def plan(self, variables=None, destroy=False, on_line=None):
        return {"summary": {"add": 1, "change": 0, "destroy": 0},
                "diff": [{"sign": "+", "type": "x", "address": f"{self.workspace}.this"}]}

    def planned_resources(self):
        return self._resources

    async def apply(self, on_line=None):
        if self.workspace in _FakeRunner.fail_applies:
            from app.tools.terraform import TerraformError
            raise TerraformError(f"provider exploded in {self.workspace}")
        _FakeRunner.applies.append(self.workspace)
        if self.workspace == "aws-vpc":
            return {"outputs": {"vpc_id": "vpc-77", "public_subnet_ids": ["sub-p1"],
                                "private_subnet_ids": ["sub-a", "sub-b"]}}
        return {"outputs": {"instance_id": "i-777"}}


class _Emitter:
    def __init__(self): self.tokens = []; self.errors = []
    async def step(self, *a, **k): pass
    async def token(self, t): self.tokens.append(t)
    async def console(self, *a, **k): pass
    async def confidentiality(self, *a, **k): pass
    async def interrupt(self, *a, **k): pass
    async def error(self, msg, **k): self.errors.append(msg)


def _dag():
    return [
        {"template_key": "aws.vpc", "inputs": {"name": "web-net", "region": "us-east-1"},
         "provides": "vpc"},
        {"template_key": "aws.ec2", "inputs": {"name": "web"},
         "wires": {"subnet_id": "public_subnet_ids[0]"}, "depends_on": "aws.vpc"},
    ]


def _state(dag):
    return {"run_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "session_id": None,
            "workflow": "governed-exec-loop", "goal_dag": dag,
            "approval_status": "approved", "user": {"region": "us-east-1"}}


@pytest.fixture
def loop_env(monkeypatch, live_redis):
    """Fake runner + captured graph/inventory writes; live Redis for step idempotency.

    Prompt 4: these tests pin the LEGACY in-process loop — with the durable-engine flag ON
    (the default posture since Prompt 3) execute_goal_dag would route into app/engine, which
    has its own coverage in test_p3_engine/test_p3_activation."""
    from app.settings import get_settings
    monkeypatch.setattr(get_settings(), "aegisops_durable_engine", "off")
    _FakeRunner.applies = []
    _FakeRunner.fail_applies = set()
    monkeypatch.setattr(exec_loop, "TerraformRunner", _FakeRunner)
    monkeypatch.setattr(exec_loop, "emitter_of", lambda cfg: cfg["emitter"])

    recorded = {"inventory": [], "graph": []}

    async def _bookkeep(step_state, template, outputs):
        recorded["inventory"].append(
            exec_loop.inventory.inventory_payload(step_state, template, outputs)["name"])
        recorded["graph"].append(template.key)
    monkeypatch.setattr(exec_loop, "_record_step_bookkeeping", _bookkeep)
    return recorded


async def test_one_approval_both_steps_applied_in_order(loop_env, live_redis):
    """Acceptance: VPC + EC2 — one approved DAG, both applied IN ORDER, outputs wired."""
    state = _state(_dag())
    out = await execute_goal_dag(state, {"emitter": _Emitter()})
    assert out["outcome"]["status"] == "applied"
    assert _FakeRunner.applies == ["aws-vpc", "aws-ec2"]          # order, exactly once each
    steps = out["outcome"]["steps"]
    assert [s["status"] for s in steps] == ["applied", "applied"]
    # The EC2 step's inputs got the VPC's REAL output subnet.
    assert out["outcome"]["outputs"]["aws.ec2"] == {"instance_id": "i-777"}
    assert loop_env["inventory"] == ["web-net", "web"]            # both recorded (D2/D3)


async def test_interrupt_replay_never_reapplies_a_done_step(loop_env, live_redis):
    """LangGraph replays the node body after a mid-node interrupt: an already-applied step must
    return its stored result (A1 claim) — never a second terraform apply."""
    state = _state(_dag())
    em = _Emitter()
    out1 = await execute_goal_dag(state, {"emitter": em})
    assert out1["outcome"]["status"] == "applied"
    applies_after_first = list(_FakeRunner.applies)

    out2 = await execute_goal_dag(state, {"emitter": em})  # simulated replay of the node body
    assert out2["outcome"]["status"] == "applied"
    assert _FakeRunner.applies == applies_after_first, "replay must not re-apply any step"


async def test_step_failure_halts_honestly_with_partial_report(loop_env, live_redis):
    _FakeRunner.fail_applies = {"aws-ec2"}
    state = _state(_dag())
    out = await execute_goal_dag(state, {"emitter": _Emitter()})
    assert out["outcome"]["status"] == "partial_failure"
    assert _FakeRunner.applies == ["aws-vpc"]                     # step 1 applied, step 2 never
    assert "step 2" in out["answer"] and "failed" in out["answer"]
    assert "provider exploded" in out["outcome"]["error"]


async def test_replan_is_a_deviation_requiring_fresh_approval(loop_env, live_redis, monkeypatch):
    """Acceptance (EFS-replan shape): a failed step that the planner revises deviates from the
    approved DAG → a fresh approval interrupt; approving it executes the REVISED step."""
    _FakeRunner.fail_applies = {"aws-ec2"}
    reapprovals: list[dict] = []

    def _replan(step, obs):
        if step["template_key"] == "aws.ec2" and step["inputs"].get("name") == "web":
            return {**step, "inputs": {**step["inputs"], "name": "web-fixed"}}
        return None

    def _reapprove(payload):
        reapprovals.append(payload)
        _FakeRunner.fail_applies = set()  # the revised step will succeed
        return {"decision": "approved", "user": "maya", "can_execute": True}

    monkeypatch.setattr(exec_loop, "_replan_step", _replan)
    monkeypatch.setattr(exec_loop, "_request_reapproval", _reapprove)

    out = await execute_goal_dag(_state(_dag()), {"emitter": _Emitter()})
    assert len(reapprovals) == 1
    dev = reapprovals[0]["plan"]["deviation"]
    assert dev["step"] == 2 and dev["now"]["name"] == "web-fixed"  # the change is SHOWN
    assert out["outcome"]["status"] == "applied"


async def test_rejected_deviation_halts_with_partial_report(loop_env, live_redis, monkeypatch):
    _FakeRunner.fail_applies = {"aws-ec2"}
    monkeypatch.setattr(exec_loop, "_replan_step",
                        lambda step, obs: {**step, "inputs": {**step["inputs"], "name": "web2"}})
    monkeypatch.setattr(exec_loop, "_request_reapproval",
                        lambda payload: {"decision": "rejected"})
    out = await execute_goal_dag(_state(_dag()), {"emitter": _Emitter()})
    assert out["outcome"]["status"] == "partial_failure"
    assert out["outcome"]["halted"] == "deviation rejected"
    assert _FakeRunner.applies == ["aws-vpc"]


async def test_replan_bound_is_enforced(loop_env, live_redis, monkeypatch):
    """MAX_REPLANS_PER_STEP: a step that keeps failing after its replan halts — no infinite loop."""
    _FakeRunner.fail_applies = {"aws-ec2"}  # never cleared — the revised step fails too
    monkeypatch.setattr(exec_loop, "_replan_step",
                        lambda step, obs: {**step, "inputs": {**step["inputs"],
                                                              "name": step["inputs"]["name"] + "x"}})
    monkeypatch.setattr(exec_loop, "_request_reapproval",
                        lambda payload: {"decision": "approved"})
    out = await execute_goal_dag(_state(_dag()), {"emitter": _Emitter()})
    assert out["outcome"]["status"] == "partial_failure"
    assert "replan bound" in out["outcome"]["halted"]
    assert MAX_REPLANS_PER_STEP == 1


async def test_failed_policy_check_on_a_step_halts_not_applies(loop_env, live_redis, monkeypatch):
    """A step whose REAL plan fails a policy check must halt the loop, never auto-apply."""
    def _failing_policy(i, resources=None):
        return [{"name": "Root volume encrypted", "passed": False, "evaluated": True,
                 "detail": "NOT encrypted"}]

    ec2 = exec_loop.templates.by_key("aws.ec2")
    monkeypatch.setattr(ec2, "policy_fn", _failing_policy)
    out = await execute_goal_dag(_state(_dag()), {"emitter": _Emitter()})
    assert out["outcome"]["status"] == "partial_failure"
    assert "policy check(s) failed" in out["outcome"]["error"]
    assert _FakeRunner.applies == ["aws-vpc"]  # the failing step never applied
