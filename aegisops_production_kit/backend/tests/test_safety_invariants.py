"""Phase-A SAFETY INVARIANTS (03_TEST_MATRIX §A) — the tests that kill the destructive class.

A1  create never destroys  — a create plan contains zero delete/replace actions; violations block.
A2  sequential creates coexist — per-resource Terraform state (TF_WORKSPACE) keeps X intact when Y applies.
A3  destroy only the named target — destroy plans contain only deletes; target comes from inventory.
A4  action-vs-operation hard guard — a forced mismatch is blocked and surfaced, never executed.
A5  read/status never mutates — a wide phrasing sweep can never carry a side-effecting action.
A6  no create↔destroy swap — both directions, at the deterministic guard layer.

Target APIs (implemented in Phase B; a missing piece fails the specific test, by design):
  app.agents.plan_guard.check_plan_actions(action, diff) -> str | None
  TerraformRunner(workspace, settings, state_workspace="res-…") + ensure_state_workspace()
  intent_guard mirror guard (destructive verb + action=create ⇒ blocked)
"""

from __future__ import annotations

import os
import shutil
import uuid

import pytest

from app.agents import intent_guard

# ── Synthetic plan diffs (same shape _summarize_plan emits) ───────────────────────────────────


def _diff(*actions):
    return [{"address": f"r{i}", "type": "t", "actions": list(a),
             "sign": "+" if a == ["create"] else "-" if a == ["delete"] else "~"}
            for i, a in enumerate(actions)]


_CREATE_ONLY = _diff(["create"], ["create"], ["no-op"], ["read"])
_HAS_DELETE = _diff(["create"], ["delete"])
_HAS_REPLACE = _diff(["create"], ["create", "delete"])      # -/+ replace
_DESTROY_ONLY = _diff(["delete"], ["delete"])
_DESTROY_WITH_CREATE = _diff(["delete"], ["create"])


# ═══ A1 / A4 — plan-inspection hard guard (pure function) ════════════════════════════════════

class TestPlanGuard:
    def test_create_plan_with_zero_deletes_passes(self):
        from app.agents.plan_guard import check_plan_actions
        assert check_plan_actions("create", _CREATE_ONLY) is None

    def test_create_plan_containing_a_delete_is_blocked(self):
        # THE screenshot-reported destructive case: a benign create whose plan would tear down
        # an existing resource. Must be blocked before the approval gate, with an explanation.
        from app.agents.plan_guard import check_plan_actions
        msg = check_plan_actions("create", _HAS_DELETE)
        assert msg is not None and "destroy" in msg.lower()

    def test_create_plan_containing_a_replace_is_blocked(self):
        from app.agents.plan_guard import check_plan_actions
        assert check_plan_actions("create", _HAS_REPLACE) is not None

    def test_destroy_plan_with_only_deletes_passes(self):
        from app.agents.plan_guard import check_plan_actions
        assert check_plan_actions("destroy", _DESTROY_ONLY) is None

    def test_destroy_plan_containing_a_create_is_blocked(self):
        from app.agents.plan_guard import check_plan_actions
        msg = check_plan_actions("destroy", _DESTROY_WITH_CREATE)
        assert msg is not None and "create" in msg.lower()

    def test_modify_plan_may_update_but_not_delete(self):
        from app.agents.plan_guard import check_plan_actions
        assert check_plan_actions("modify", _diff(["update"])) is None
        assert check_plan_actions("modify", _HAS_DELETE) is not None
        # A replace on modify silently destroys the instance — surface it, don't run it.
        assert check_plan_actions("modify", _HAS_REPLACE) is not None

    def test_read_must_never_reach_a_plan_at_all(self):
        from app.agents.plan_guard import check_plan_actions
        assert check_plan_actions("read", _CREATE_ONLY) is not None
        assert check_plan_actions("read", _DESTROY_ONLY) is not None


# ═══ A2 — per-resource Terraform state isolation (real terraform, no cloud creds) ════════════

_TF = shutil.which("terraform")


@pytest.mark.skipif(not _TF, reason="terraform binary not on PATH (run via `make test` in-container)")
class TestStateIsolation:
    async def test_two_creates_coexist_with_distinct_state(self):
        """Create X then Y in the SAME module: Y's apply must not touch X's resources.
        This is the invariant whose absence deleted a real instance (N-08 direction 1)."""
        from app.settings import get_settings
        from app.tools.terraform import TerraformRunner

        settings = get_settings()
        tag = uuid.uuid4().hex[:6]
        ws_x, ws_y = f"res-itest-x-{tag}", f"res-itest-y-{tag}"
        rx = TerraformRunner("demo-null", settings, state_workspace=ws_x)
        ry = TerraformRunner("demo-null", settings, state_workspace=ws_y)

        async def quiet(stream, line):
            pass

        try:
            await rx.init(quiet)
            await rx.ensure_state_workspace()
            await rx.plan({"resource_name": f"x-{tag}", "replica_count": 1}, on_line=quiet)
            await rx.apply(quiet)
            x_state_before = await rx.state_list()
            assert x_state_before, "X must exist in its own state after apply"

            await ry.ensure_state_workspace()
            plan_y = await ry.plan({"resource_name": f"y-{tag}", "replica_count": 1}, on_line=quiet)
            # Y's plan must be pure create — it must NOT see X's resources (shared-state defect
            # showed them as replaces/destroys).
            assert plan_y["summary"]["destroy"] == 0
            assert all("delete" not in rc["actions"] for rc in plan_y["diff"])
            await ry.apply(quiet)

            # Both alive, in DISTINCT state files; X untouched by Y's apply.
            assert await rx.state_list() == x_state_before
            assert await ry.state_list()
            base = os.path.join(settings.terraform_workspaces_dir, "demo-null", "terraform.tfstate.d")
            assert os.path.isfile(os.path.join(base, ws_x, "terraform.tfstate"))
            assert os.path.isfile(os.path.join(base, ws_y, "terraform.tfstate"))
        finally:
            for r in (rx, ry):
                try:
                    await r.destroy({}, quiet)
                except Exception:  # noqa: BLE001 - teardown is best-effort
                    pass

    async def test_destroy_in_one_workspace_leaves_the_other_alone(self):
        """A3 at the state layer: destroying Y must not destroy X (shared state destroyed
        whatever happened to be in the file, regardless of the name the user gave)."""
        from app.settings import get_settings
        from app.tools.terraform import TerraformRunner

        settings = get_settings()
        tag = uuid.uuid4().hex[:6]
        rx = TerraformRunner("demo-null", settings, state_workspace=f"res-itest-a-{tag}")
        ry = TerraformRunner("demo-null", settings, state_workspace=f"res-itest-b-{tag}")

        async def quiet(stream, line):
            pass

        try:
            for r, n in ((rx, "a"), (ry, "b")):
                await r.init(quiet)
                await r.ensure_state_workspace()
                await r.plan({"resource_name": f"{n}-{tag}", "replica_count": 1}, on_line=quiet)
                await r.apply(quiet)
            await ry.destroy({"resource_name": f"b-{tag}", "replica_count": 1}, quiet)
            assert await rx.state_list(), "destroying B must not empty A's state"
            assert not await ry.state_list()
        finally:
            for r in (rx, ry):
                try:
                    await r.destroy({}, quiet)
                except Exception:  # noqa: BLE001
                    pass


# ═══ A2 support — state slugs are stable, distinct, and filesystem-safe ══════════════════════

def test_state_slug_stable_distinct_and_safe():
    from app.tools.terraform import state_slug
    assert state_slug("web-01") == state_slug("web-01")            # deterministic
    assert state_slug("web-01") != state_slug("web-02")            # distinct per name
    assert state_slug("My Bucket!!") == "res-my-bucket"            # sanitized
    assert state_slug("") == "res-unnamed"
    assert len(state_slug("x" * 300)) <= 60                        # bounded
    assert state_slug("Web_01") == state_slug("web_01")            # case-insensitive


# ═══ A6 — no create↔destroy swap at the deterministic guard layer (both directions) ══════════

_CREATE_PHRASES = [
    "create a new vm in aws", "provision an s3 bucket", "spin up a new instance in gcp",
    "launch an ubuntu server in azure", "set up a database in aws", "make a new bucket please",
]
_DESTROY_PHRASES = [
    "destroy the vm sai-test", "delete the s3 bucket sai2792002-bucket", "terminate the instance web-01",
    "tear down the gcp vm", "remove the vpc prod-network", "please destroy the previous instance",
]


class TestNoSwap:
    @pytest.mark.parametrize("msg", _DESTROY_PHRASES)
    def test_destroy_phrased_request_can_never_run_as_create(self, msg):
        # Mirror guard (new in Phase B): if the LLM misclassifies an explicitly destructive
        # message as CREATE, the guard must block/redirect — never let it provision.
        g = intent_guard.guard_classification(
            msg, {"domain": "cloudops", "action": "create", "intent": "create_ec2", "resource": "ec2"})
        assert g is not None, f"destroy-phrased message ran as create: {msg!r}"
        assert g.get("action") != "create"
        assert g.get("needs_clarification") or g.get("action") == "destroy"

    @pytest.mark.parametrize("msg", _DESTROY_PHRASES)
    def test_destroy_phrased_request_classified_destroy_is_allowed(self, msg):
        g = intent_guard.guard_classification(
            msg, {"domain": "cloudops", "action": "destroy", "intent": "destroy_x", "resource": "ec2"})
        assert g is None  # explicit verb + destroy classification → untouched

    @pytest.mark.parametrize("msg", _CREATE_PHRASES)
    def test_create_phrased_request_classified_destroy_is_blocked(self, msg):
        # Existing Phase-7 guard direction, locked in across more phrasings.
        g = intent_guard.guard_classification(
            msg, {"domain": "cloudops", "action": "destroy", "intent": "destroy_x", "resource": "ec2"})
        assert g is not None
        assert g.get("action") != "destroy"

    @pytest.mark.parametrize("msg", _CREATE_PHRASES)
    def test_create_phrased_request_classified_create_is_untouched(self, msg):
        g = intent_guard.guard_classification(
            msg, {"domain": "cloudops", "action": "create", "intent": "create_x", "resource": "ec2"})
        assert g is None


# ═══ A5 — read/status phrasing sweep never carries a side-effecting action ═══════════════════

_READ_SWEEP = [
    "how many vms are running in aws?", "how many s3 buckets do we have?",
    "are any instances up in azure?", "is there a database in gcp?", "what is the vpc id of test-vm?",
    "which region is web-01 in?", "did i create any resources today?", "do i have any clusters?",
    "list my buckets", "list all vms in gcp", "show me the security group of sai-test",
    "status of the rds instance", "describe the vm i created", "count my vpcs",
    "what's the public ip of web-01?", "tell me about my infrastructure",
    "is the instance i created yesterday still running?", "have i provisioned anything in azure?",
    "what did you create for me?", "are there any failed deployments?",
    "whats running right now", "show status", "did the bucket get created?",
    "is my vm healthy?", "how much storage do we use in s3?",
]


@pytest.mark.parametrize("msg", _READ_SWEEP)
def test_read_phrasing_sweep_never_mutates(msg):
    assert intent_guard.is_question(msg), f"not detected as a question: {msg!r}"
    for bad_action, bad_intent in (("create", "create_x"), ("destroy", "destroy_x"), ("modify", "modify_x")):
        g = intent_guard.guard_classification(
            msg, {"domain": "cloudops", "action": bad_action, "intent": bad_intent, "resource": "ec2"})
        assert g is not None, f"{bad_action} misfire not downgraded for: {msg!r}"
        assert g["action"] == "read"


# ── A2: the approval node re-asserts plan_guard at the choke-point ────────────────────────────
# Even if a plan node forgot to call the guard, the approval node must refuse a mismatched plan
# BEFORE the durable interrupt — never show it to an approver, never apply it.


class TestApprovalChokePointGuard:
    def _cfg(self):
        from app.agents.events import Emitter, RunChannel
        return {"configurable": {"emitter": Emitter(RunChannel("a2-run"))}}

    def _state(self, **kw):
        base = {"run_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "needs_change": True,
                "approval_status": "pending", "interrupt_payload": {"kind": "approval"}}
        base.update(kw)
        return base

    async def test_apply_plan_with_delete_is_blocked_before_interrupt(self):
        from app.agents.approval import approval
        # A plan node that "forgot" the guard: apply-mode, but the diff would replace a resource.
        out = await approval(self._state(execution_mode="apply", diff=_HAS_REPLACE), self._cfg())
        assert out["approval_status"] == "blocked"
        assert out["outcome"]["status"] == "blocked"
        assert "safety guard" in out["answer"].lower()

    async def test_destroy_plan_that_would_create_is_blocked(self):
        from app.agents.approval import approval
        out = await approval(self._state(execution_mode="destroy", diff=_DESTROY_WITH_CREATE), self._cfg())
        assert out["approval_status"] == "blocked"

    async def test_explicit_action_in_state_is_honored(self):
        from app.agents.approval import approval
        out = await approval(self._state(action="read", execution_mode="apply", diff=_CREATE_ONLY),
                             self._cfg())
        assert out["approval_status"] == "blocked", "a read must never carry a plan"

    async def test_blocked_plan_routes_away_from_execute(self):
        from app.agents.approval import approval_decision
        assert approval_decision({"approval_status": "blocked"}) == "finalize"
