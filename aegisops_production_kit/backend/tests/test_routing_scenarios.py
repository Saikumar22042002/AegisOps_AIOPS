"""Scenario coverage for CloudOps routing (6.2) — the "hundreds of cases" concern.

The router's LLM classification produces (domain, cloud, resource, action, target). The
*deterministic* routing surface downstream of that classification is:

  resolve_cloud(state)          → the target cloud (explicit > UI selector > resource hint > ask)
  templates.select(cloud, res)  → exactly one curated module, or None (→ honest clarification)

These two functions decide "correct cloud + service + module" for every request, so they are
exercised here across every representative category (create compute/storage/db/k8s/network on
AWS/Azure/GCP, with word synonyms), plus the two must-not-fail-silently cases: ambiguous cloud
(→ ask) and unsupported combo (→ clarify, never wrong-cloud). Parameter and plan correctness
per category are covered in test_params_scenarios.py; the LLM classification step itself is
covered live in test_llm_classification.py (skips without a Gemini key).
"""

from __future__ import annotations

import pytest

from app.agents import templates
from app.agents.approval import approval_decision
from app.agents.cloudops import resolve_cloud
from app.agents.router import route_decision


def _state(cloud=None, resource=None, ui_cloud=None):
    return {"cloud": cloud, "resource": resource, "user": {"cloud": ui_cloud} if ui_cloud else {}}


# (cloud_named, resource, expected_template_key) — the module the request must resolve to.
# Covers every cell of the supported matrix, in both canonical and synonym phrasings.
_SUPPORTED = [
    # ── AWS ──
    ("aws", "ec2", "aws.ec2"), ("aws", "vm", "aws.ec2"), ("aws", "instance", "aws.ec2"),
    ("aws", "server", "aws.ec2"), ("aws", "compute", "aws.ec2"),
    ("aws", "s3", "aws.s3"), ("aws", "bucket", "aws.s3"), ("aws", "object_storage", "aws.s3"),
    ("aws", "rds", "aws.rds"), ("aws", "database", "aws.rds"), ("aws", "db", "aws.rds"),
    ("aws", "postgres", "aws.rds"), ("aws", "mysql", "aws.rds"),
    ("aws", "eks", "aws.eks"), ("aws", "k8s", "aws.eks"), ("aws", "kubernetes", "aws.eks"),
    ("aws", "cluster", "aws.eks"),
    ("aws", "vpc", "aws.vpc"), ("aws", "network", "aws.vpc"),
    # ── Azure ──
    ("azure", "vm", "azure.vm"), ("azure", "instance", "azure.vm"), ("azure", "ec2", "azure.vm"),
    ("azure", "compute", "azure.vm"),
    ("azure", "storage", "azure.storage"), ("azure", "blob", "azure.storage"),
    ("azure", "bucket", "azure.storage"), ("azure", "storage_account", "azure.storage"),
    ("azure", "postgres", "azure.postgres"), ("azure", "database", "azure.postgres"),
    ("azure", "db", "azure.postgres"), ("azure", "sql", "azure.postgres"),
    ("azure", "aks", "azure.aks"), ("azure", "k8s", "azure.aks"), ("azure", "kubernetes", "azure.aks"),
    ("azure", "cluster", "azure.aks"),
    ("azure", "resource_group", "azure.resource_group"), ("azure", "rg", "azure.resource_group"),
    # ── GCP ──
    ("gcp", "vm", "gcp.vm"), ("gcp", "gce", "gcp.vm"), ("gcp", "instance", "gcp.vm"),
    ("gcp", "compute", "gcp.vm"), ("gcp", "ec2", "gcp.vm"),
    ("gcp", "gcs", "gcp.gcs"), ("gcp", "bucket", "gcp.gcs"), ("gcp", "blob", "gcp.gcs"),
    ("gcp", "cloudsql", "gcp.cloudsql"), ("gcp", "database", "gcp.cloudsql"),
    ("gcp", "postgres", "gcp.cloudsql"), ("gcp", "sql", "gcp.cloudsql"),
    ("gcp", "gke", "gcp.gke"), ("gcp", "k8s", "gcp.gke"), ("gcp", "cluster", "gcp.gke"),
]


@pytest.mark.parametrize("cloud,resource,expected_key", _SUPPORTED)
def test_supported_category_routes_to_correct_module(cloud, resource, expected_key):
    resolved, _reason = resolve_cloud(_state(cloud=cloud, resource=resource))
    assert resolved == cloud, f"{cloud}/{resource} should resolve to {cloud}"
    t = templates.select(resolved, resource)
    assert t is not None, f"{cloud}/{resource} must select a module"
    assert t.key == expected_key
    assert t.cloud == cloud  # never cross-cloud


def test_resolve_cloud_priority_named_beats_ui_and_hint():
    # Cloud named in the request wins over the UI selector…
    assert resolve_cloud(_state(cloud="gcp", resource="vm", ui_cloud="AWS")) == ("gcp", "named in request")
    # …and over a resource-brand hint (s3 hints AWS, but a named GCP request stays GCP so it can
    # then honestly clarify "no gcp/s3 module" rather than silently provisioning on AWS).
    assert resolve_cloud(_state(cloud="gcp", resource="s3"))[0] == "gcp"
    assert templates.select("gcp", "s3") is None


def test_resolve_cloud_ui_selector_used_when_request_silent():
    resolved, reason = resolve_cloud(_state(resource="vm", ui_cloud="Azure"))
    assert resolved == "azure" and reason == "UI cloud selector"
    assert templates.select(resolved, "vm").key == "azure.vm"


@pytest.mark.parametrize("resource,expected_cloud", [
    ("s3", "aws"), ("rds", "aws"), ("vpc", "aws"), ("eks", "aws"),
    ("storage", "azure"), ("resource_group", "azure"), ("gcs", "gcp"),
])
def test_resource_brand_hint_when_no_cloud_named(resource, expected_cloud):
    # Only cloud-branded service names hint a cloud (no request/UI cloud given).
    resolved, reason = resolve_cloud(_state(resource=resource))
    assert resolved == expected_cloud
    assert "inferred from resource" in reason


@pytest.mark.parametrize("resource", ["vm", "instance", "compute", "server", "database", "db", "cluster", "k8s"])
def test_ambiguous_cloud_asks_never_defaults_to_aws(resource):
    # A generic (non-cloud-branded) resource with no named cloud and no UI selector is genuinely
    # cross-cloud → resolve_cloud must return None so the agent ASKS (never silently picks AWS).
    resolved, reason = resolve_cloud(_state(resource=resource))
    assert resolved is None
    assert reason == "ambiguous"


@pytest.mark.parametrize("cloud,resource", [
    ("aws", "storage"),          # "storage" is Azure-branded — no aws.storage module
    ("aws", "module"),           # generic runtime-HCL escape hatch was removed (2.3)
    ("gcp", "resource_group"),   # Azure concept
    ("gcp", "lambda"),           # serverless concept — never a curated module here
    ("azure", "gcs"),            # GCP concept
    ("aws", "aks"),              # Azure k8s on AWS — nonsense
])
def test_unsupported_combo_returns_none_for_honest_clarification(cloud, resource):
    assert templates.select(cloud, resource) is None


def test_no_template_is_cross_cloud():
    # Structural guarantee: every module's key prefix matches its cloud.
    for t in templates.TEMPLATES:
        assert t.key.split(".")[0] == t.cloud


def test_route_decision_edges():
    assert route_decision({"domain": "cloudops"}) == "cloudops"
    assert route_decision({"domain": "sre"}) == "sre"
    assert route_decision({"domain": "knowledge"}) == "knowledge"
    # Ambiguous intent is diverted to a clarification (general), never to a side-effecting agent.
    assert route_decision({"domain": "cloudops", "needs_clarification": True}) == "general"
    assert route_decision({}) == "general"


def test_approval_decision_gate():
    assert approval_decision({"approval_status": "approved"}) == "execute"
    assert approval_decision({"approval_status": "rejected"}) == "finalize"
    assert approval_decision({"approval_status": "not_required"}) == "finalize"
    assert approval_decision({}) == "finalize"  # default-safe: no approval → no execution


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 7 / BUG-01 — router safety: a read/status question can NEVER enter a destructive
# workflow, and a pending param-collection can never hijack a new question/request.
# The three prompts below are the exact messages from screenshots 18/19/20.
# ═══════════════════════════════════════════════════════════════════════════════════════════
from app.agents import intent_guard  # noqa: E402

_SCREENSHOT_20 = "How many s3 buckets are ruunning in aws ?"
_SCREENSHOT_19 = "Are any instance up and ruunning in azure or gcp ?"
_SCREENSHOT_18 = "How many instances are up and running in the aws right now ?"
_READ_PROMPTS = [_SCREENSHOT_18, _SCREENSHOT_19, _SCREENSHOT_20,
                 "Did i create any resources in aws or azure or gcp ?",
                 "Earlier, Did I create any resources, Are any succcesfully created or any failed ?",
                 "What is the instance size of the Vm which i created earlier in aws ?",
                 "Did the previous s3 bucket I created, Is it created ?",
                 "list my vms", "show me the vpc id of test-vm", "status of sai-test"]


@pytest.mark.parametrize("msg", _READ_PROMPTS)
def test_read_questions_are_question_shaped(msg):
    assert intent_guard.is_question(msg), f"should be a question: {msg!r}"


@pytest.mark.parametrize("msg", _READ_PROMPTS)
def test_guard_downgrades_any_destructive_misfire_to_read(msg):
    # Simulate the exact LLM misfire from screenshots 19/20: destroy_vpc for a read question.
    g = intent_guard.guard_classification(msg, {"domain": "cloudops", "action": "destroy",
                                                "intent": "destroy_vpc", "resource": "vpc"})
    assert g is not None
    assert g["action"] == "read"
    assert g["intent"].startswith("query_")
    assert not g.get("needs_clarification")  # question → answered read-only, not bounced
    # A create-misfire on a question is downgraded the same way.
    g2 = intent_guard.guard_classification(msg, {"domain": "cloudops", "action": "create",
                                                 "intent": "provision_s3_bucket", "resource": "s3"})
    assert g2 is not None and g2["action"] == "read"


def test_explicit_destroy_is_still_allowed():
    g = intent_guard.guard_classification(
        "destroy the vpc named prod-network",
        {"domain": "cloudops", "action": "destroy", "intent": "destroy_vpc", "resource": "vpc"})
    assert g is None  # explicit destructive verb + not a question → untouched
    assert intent_guard.explicitly_destructive("please tear down the test cluster")
    assert not intent_guard.explicitly_destructive("how many clusters are running?")


def test_destroy_without_explicit_verb_is_stopped():
    # Not a question, but no destructive verb either → clarification, action forced to read.
    g = intent_guard.guard_classification(
        "clean the vpc prod-network please",
        {"domain": "cloudops", "action": "destroy", "intent": "destroy_vpc", "resource": "vpc"})
    assert g is not None
    assert g["needs_clarification"] is True
    assert g["action"] == "read"


def test_polite_create_and_day2_modify_are_untouched():
    # Screenshot 17 (working day-2 modify) and polite create requests must NOT be downgraded.
    modify = "Can you open the port inbound 8002 in the sai-test vm which i've created earlier in aws ?"
    assert not intent_guard.is_question(modify)
    assert intent_guard.guard_classification(
        modify, {"domain": "cloudops", "action": "modify", "intent": "modify_ec2", "resource": "ec2"}) is None
    create = "Can you create a Vm in gcp ?"
    assert not intent_guard.is_question(create)
    assert intent_guard.guard_classification(
        create, {"domain": "cloudops", "action": "create", "intent": "create_gcp_vm", "resource": "vm"}) is None


def test_guard_only_applies_to_cloudops():
    assert intent_guard.guard_classification("how are you?", {"domain": "general", "action": "create",
                                                              "intent": "chitchat"}) is None


def test_pending_hijack_new_requests_reclassified():
    # These messages must NEVER be swallowed as answers to a pending param collection
    # (screenshots 19/20 were hijacked by a stale destroy_vpc pending record).
    for msg in [_SCREENSHOT_19, _SCREENSHOT_20, "Did i create any resources in aws or azure or gcp ?",
                "Create a Vm in gcp", "Provision an S3 bucket in AWS us-east-1",
                "can you open port 8002 on the sai-test vm", "cancel"]:
        assert intent_guard.is_new_request(msg), f"should re-classify: {msg!r}"


def test_pending_param_answers_still_continue():
    # Real param answers from the working screenshots (1/4/8/9) must continue collection.
    for msg in ["ok, ubuntu", "sai-test, t3.micro, ubuntu, create one", "my-bucket",
                "test-v1, ec2-micro, ubuntu", "sai-v1, Standard_D2s_v5 (2vcpu), windows",
                "name web-01, t3.small, ubuntu, key my-key", "create", "ubuntu-22.04",
                "use subnet subnet-009a55b7fa8721333", "web-01"]:
        assert not intent_guard.is_new_request(msg), f"should continue collection: {msg!r}"


def test_broad_inventory_question_detected():
    assert intent_guard.is_broad_inventory_question("Did i create any resources in aws or azure or gcp ?")
    assert intent_guard.is_broad_inventory_question("list all my resources")
    assert not intent_guard.is_broad_inventory_question("what is the vpc id of test-vm")
    assert not intent_guard.is_broad_inventory_question("create a vm in gcp")
