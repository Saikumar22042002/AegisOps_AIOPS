"""Forensic-audit remediation pins (2026-08-16) — production correctness.

Each test pins one proven-live defect closed by this change set:
  D-1  "remove port 8501" planned as OPEN and a 0/0/0 plan reported applied=true
  D-2  failed applies orphaned real infra invisible to inventory (wedged workspaces)
  D-3  aws.vpc placed subnets in Local Zones (opt-in AZs sorted first)
  D-4  a VPC named in the message never bound; the default VPC was unreachable
  D-5  "resources in AWS" listed GCP rows; ghosts rendered as active
  D-6  "who approved / what changed" misrouted (devops) or unanswerable
  D-7  a failed run forgot every collected parameter
"""

from __future__ import annotations

import pytest

from app.agents import dependency, intent_guard, plan_guard
from app.agents.cloudops import _MODIFY_CAPS, _apply_modification, _history_window
from app.agents.router import apply_post_guard_rules


# ── D-1: verb correctness + zero-change honesty ───────────────────────────────────────────

def test_close_port_subtracts_never_unions():
    merged, desc = _apply_modification(
        {"name": "web", "ingress_ports": [80, 8501]}, {"ingress_ports_remove": [8501]})
    assert merged["ingress_ports"] == [80]
    assert any("close inbound TCP [8501]" in d for d in desc)


def test_close_wins_over_simultaneous_open():
    merged, _ = _apply_modification(
        {"name": "web", "ingress_ports": []},
        {"ingress_ports": [8501], "ingress_ports_remove": [8501]})
    assert merged["ingress_ports"] == []


def test_closing_an_unopened_port_is_an_honest_noop():
    base = {"name": "web", "ingress_ports": [80]}
    merged, desc = _apply_modification(base, {"ingress_ports_remove": [9999]})
    assert merged == base  # input-level no-op → the caller reports NO_CHANGE
    assert any("not open" in d for d in desc)


def test_opening_an_open_port_is_an_honest_noop():
    base = {"name": "web", "ingress_ports": [8501]}
    merged, desc = _apply_modification(base, {"ingress_ports": [8501]})
    assert merged == base
    assert any("already open" in d for d in desc)


def test_open_still_unions_never_replaces():  # the pre-existing pin, preserved
    merged, desc = _apply_modification(
        {"name": "web", "ingress_ports": [80]}, {"ingress_ports": [8080]})
    assert merged["ingress_ports"] == [80, 8080]
    assert any("open inbound TCP [8080]" in d for d in desc)


def test_remove_capability_is_declared():
    for key in ("aws.ec2", "gcp.vm", "azure.vm"):
        assert "ingress_ports_remove" in _MODIFY_CAPS[key]


def test_modify_may_delete_rule_resources_only():
    """Closing a port deletes its RULE resource — allowed; deleting real infra still halts."""
    rule_diff = [{"address": "aws_vpc_security_group_ingress_rule.app[\"8501\"]",
                  "actions": ["delete"]}]
    assert plan_guard.check_plan_actions("modify", rule_diff) is None
    infra_diff = [{"address": "aws_instance.this", "actions": ["delete"]}]
    assert plan_guard.check_plan_actions("modify", infra_diff) is not None
    mixed = rule_diff + infra_diff
    v = plan_guard.check_plan_actions("modify", mixed)
    assert v is not None and "aws_instance.this" in v


def test_zero_change_guard():
    assert plan_guard.zero_change({"add": 0, "change": 0, "destroy": 0})
    assert plan_guard.zero_change({})
    assert plan_guard.zero_change(None)
    assert not plan_guard.zero_change({"add": 1, "change": 0, "destroy": 0})
    assert not plan_guard.zero_change({"add": 0, "change": 0, "destroy": 2})


async def test_close_verbs_never_become_open(monkeypatch):
    """The deterministic direction guard: a close-verbed message without an open verb can
    only ever CLOSE — whatever the model returned (here: the model inverts, the guard fixes)."""
    from app.agents import cloudops

    monkeypatch.setattr(cloudops.llm_service, "configured", lambda *a, **k: True)

    async def _model_inverts(settings, prompt, message, **kw):
        return {"ingress_ports": [8501]}  # the live defect: remove extracted as open

    monkeypatch.setattr(cloudops.llm, "classify_json", _model_inverts)
    changes = await cloudops._extract_modification(object(), "Remove inbound port 8501 from MySource.")
    assert changes.get("ingress_ports_remove") == [8501]
    assert "ingress_ports" not in changes


async def test_port_fallback_extracts_direction_without_llm(monkeypatch):
    from app.agents import cloudops

    monkeypatch.setattr(cloudops.llm_service, "configured", lambda *a, **k: False)
    close = await cloudops._extract_port_changes(object(), "close port 8501 on MySource")
    assert close == {"open": [], "close": [8501]}
    open_ = await cloudops._extract_port_changes(object(), "open ports 80 and 443")
    assert open_ == {"open": [80, 443], "close": []}


# ── D-4: dependency binding — named parents + reachable default ───────────────────────────

_CANDS = [{"name": "audit-net", "provider_id": "vpc-04c5"},
          {"name": "aegisops-test-vpc", "provider_id": "vpc-04c7"}]


def test_parent_named_in_message_binds():
    named = dependency._candidates_named_in(
        "Create a t2.micro EC2 named MySource in the audit-net VPC", _CANDS)
    assert [c["name"] for c in named] == ["audit-net"]


def test_unnamed_message_binds_nothing():
    assert dependency._candidates_named_in("Create an EC2 instance named MySource", _CANDS) == []


def test_reply_default_maps_to_default_choice():
    ask = {"parent_type": "vpc", "options": _CANDS}
    got = dependency.choice_from_reply("Use the account default VPC, not an inventoried one.", ask)
    assert got == {"parent_type": "vpc", "choice": "__default__"}
    got2 = dependency.choice_from_reply("default", ask)
    assert got2 == {"parent_type": "vpc", "choice": "__default__"}  # the menu's own word works


def test_resolver_honors_default_choice():
    active = [{"cloud": "aws", "resource_type": "vpc", **c,
               "attributes": {"public_subnet_ids": ["subnet-1"]}} for c in _CANDS]
    closure = dependency.resolve_closure(
        "aws.ec2", {"name": "vm1"}, active, message="",
        dep_choice={"parent_type": "vpc", "choice": "__default__"})
    assert closure.status == "complete"
    assert any("defaulting to" in n for n in closure.notes)
    assert "subnet_id" not in closure.inputs  # the module auto-resolves the default subnet


def test_resolver_binds_parent_named_in_request():
    active = [{"cloud": "aws", "resource_type": "vpc", **c,
               "attributes": {"public_subnet_ids": [f"subnet-{c['name']}"]}} for c in _CANDS]
    closure = dependency.resolve_closure(
        "aws.ec2", {"name": "vm1"}, active,
        message="Create a t2.micro EC2 named vm1 in the audit-net VPC in a public subnet")
    assert closure.status == "complete"
    assert closure.inputs.get("subnet_id") == "subnet-audit-net"


def test_resolver_still_asks_when_ambiguous():
    active = [{"cloud": "aws", "resource_type": "vpc", **c,
               "attributes": {"public_subnet_ids": ["s"]}} for c in _CANDS]
    closure = dependency.resolve_closure("aws.ec2", {"name": "vm1"}, active,
                                         message="Create an EC2 named vm1")
    assert closure.status == "ask"
    assert "default" in closure.question  # the default is now OFFERED in the menu


# ── D-7: parameter continuity — tf-vars round-trip back to collector vocabulary ───────────

def test_ec2_tf_vars_round_trip_to_spec_values():
    from app.agents import params as p
    tf = {"name": "FixProbe", "instance_type": "t3.micro", "os": "amazon-linux-2023",
          "key_name": "FixProbe-key", "create_key_pair": True, "allowed_cidr": "",
          "region": "us-east-1"}
    spec = p.from_tf_vars("aws.ec2", tf)
    assert spec["key_pair"] == "create" and "key_name" not in spec
    assert spec["allowed_cidr"] == "none"  # explicit closed-access answer survives
    # And the collector's forward transform reproduces the key decision.
    back = p.to_tf_vars("aws.ec2", spec)
    assert back["create_key_pair"] is True
    tf2 = {"name": "X", "key_name": "existing-kp", "create_key_pair": False}
    assert p.from_tf_vars("aws.ec2", tf2)["key_pair"] == "existing-kp"


# ── D-6: provenance/history detection + deterministic routing ─────────────────────────────

def test_destroy_of_named_resource_is_a_new_request_not_an_answer():
    """Found live 2026-08-17: 'Destroy FixProbe completely.' was consumed as a pending
    collection's next answer (the VPC menu re-asked) because the shape detector required a
    GENERIC resource noun after the verb. A destructive-verb start is always a new request."""
    assert intent_guard.message_shape("Destroy FixProbe completely.") == "request"
    assert intent_guard.message_shape("delete my-bucket-42") == "request"
    assert intent_guard.message_shape("t3.micro, create, none") == "answer"  # answers unchanged
    assert intent_guard.message_shape("ubuntu-22.04") == "answer"


def test_provenance_and_history_detection():
    assert intent_guard.is_provenance_question("Who approved the run that created MySource?")
    assert intent_guard.is_history_question("What did I change yesterday?")
    assert intent_guard.is_history_question("What was the previous configuration of MySource?")
    assert intent_guard.is_history_question("What ports did I open on MySource, and when?")
    assert not intent_guard.is_history_question("Create an EC2 instance named MySource")
    assert not intent_guard.is_provenance_question("open port 8501 on MySource")


def test_router_guard_routes_provenance_to_audit_record():
    updates = {"domain": "devops", "action": "create", "intent": "get_run_approval_details",
               "target": "MySource", "routing_reason": ""}
    out = apply_post_guard_rules(updates, "Who approved the run that created MySource?", 0.9)
    assert out["domain"] == "cloudops" and out["action"] == "read"
    assert out["target"] == "MySource"  # the target survives the reroute


def test_router_guard_leaves_ordinary_requests_alone():
    updates = {"domain": "cloudops", "action": "create", "intent": "create_ec2",
               "target": None, "routing_reason": ""}
    out = apply_post_guard_rules(updates, "Create an EC2 instance named web-01", 0.9)
    assert out["action"] == "create" and out["domain"] == "cloudops"


def test_history_window_parsing():
    w = _history_window("what did I change yesterday?")
    assert w is not None and (w[1] - w[0]).days <= 1
    assert _history_window("what changed on MySource?") is None


# ── D-3: the VPC template selects standard AZs only ───────────────────────────────────────

def test_vpc_template_filters_optin_zones():
    from pathlib import Path
    candidates = [Path(__file__).resolve().parents[2] / "infra/terraform-workspaces/aws-vpc/main.tf",
                  Path("/app/.terraform-state/tfdata/aws-vpc/main.tf")]
    tf = next((p for p in candidates if p.exists()), None)
    if tf is None:  # terraform workspaces not present in this environment (image-only run)
        pytest.skip("aws-vpc template not mounted in this environment")
    text = tf.read_text(encoding="utf-8")
    assert "opt-in-status" in text and "opt-in-not-required" in text
    assert "map_public_ip_on_launch" in text


# ── D-2/D-5: inventory statuses are honest (pure surface) ─────────────────────────────────

def test_revision_action_vocabulary_matches_migration():
    from alembic.config import Config  # noqa: F401 - import proves alembic is present
    from pathlib import Path
    mig = Path(__file__).resolve().parents[1] / "alembic/versions/0016_resource_revisions.py"
    text = mig.read_text(encoding="utf-8")
    for action in ("created", "modified", "destroyed", "failed", "partial",
                   "orphaned", "no_change", "unknown"):
        assert f"'{action}'" in text or f'"{action}"' in text


def test_resource_revision_model_is_append_only_shape():
    from app.db.models import ResourceRevision
    cols = {c.name for c in ResourceRevision.__table__.columns}
    assert {"org_id", "name", "cloud", "action", "before_state", "after_state",
            "actor_user", "run_id", "session_id", "execution_result"} <= cols
