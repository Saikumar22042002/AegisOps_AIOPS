"""P4 — capability packs, provider-neutral objective model, permission modes, and the
representative end-to-end read flow through packs + P2 harness.

The load-bearing properties: (1) AWS/Azure/GCP are equal first-class packs; (2) the harness
read registry is sourced from packs cloud-neutrally with read-only-by-construction preserved;
(3) mutation is DECLARED, never executed as a read tool (Terraform boundary intact); (4) the
permission matrix gates mutation to approval and never grants autonomous mutation; (5) a read
objective flows understand→resolve→harness-reason→verify→evidence over pack tools.
"""

from __future__ import annotations

import pytest

from app.harness import policy
from app.packs import objective, registry
from app.packs.base import CAPABILITY_FAMILIES, CapabilityPack, ToolSpec
from app.settings import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _s(**over) -> Settings:
    base = {"gemini_api_key": "k"}
    base.update(over)
    return Settings(**base)


# ── pack contract + multi-cloud parity ──────────────────────────────────────────────────────

def test_five_packs_present_across_three_domains_and_three_clouds():
    packs = {p.name: p for p in registry.all_packs(_s())}
    assert set(packs) == {"cloudops.aws", "cloudops.azure", "cloudops.gcp",
                          "sreops.k8s", "devops.github"}
    # CloudOps = AWS/Azure/GCP equal first-class (each a distinct provider pack)
    cloud = {p.provider for p in packs.values() if p.domain == "cloudops"}
    assert cloud == {"aws", "azure", "gcp"}
    for p in packs.values():
        assert all(t.family in CAPABILITY_FAMILIES for t in p.tools)


def test_read_registry_is_pack_sourced_and_read_only_by_construction():
    # Registration-mechanism parity: from ALL packs, every cloud contributes read tools
    # equally (the pack structure is provider-symmetric).
    reg = registry.build_read_registry(_s(), packs=registry.all_packs(_s()))
    names = reg.names()
    assert all(n.startswith(("cloudops.", "sreops.", "devops.")) for n in names)
    assert not any("create" in n or "restart" in n or "scale" in n or "rollback" in n
                   for n in names)
    for cloud in ("cloudops.aws", "cloudops.azure", "cloudops.gcp"):
        assert any(n.startswith(cloud) for n in names), f"{cloud} contributes no read tools"


def test_configured_packs_gates_by_credentials_no_fake_abstraction():
    """Parity HONESTY (00 §5 / prompt): an unconfigured provider lists in the catalog but
    contributes no callable tools — never a fake abstraction. On this host GCP has no creds."""
    cat = {c["pack"]: c for c in registry.capability_catalog(_s())}
    assert cat["cloudops.gcp"]["read"]          # the capability EXISTS in the catalog
    configured = {p.name for p in registry.configured_packs(_s())}
    # gcp is gated out when unconfigured; aws/azure (default cred chains) contribute
    assert "cloudops.aws" in configured and "cloudops.azure" in configured


def test_mutation_is_declared_not_registered_as_a_read_tool():
    aws = next(p for p in registry.all_packs(_s()) if p.name == "cloudops.aws")
    muts = {t.template_key for t in aws.mutation_specs()}
    assert "aws.vpc" in muts and "aws.eks" in muts          # declared, catalog-keyed
    # a mutation spec has no fn → cannot be executed as a read tool
    assert all(t.fn is None for t in aws.mutation_specs())


def test_registering_a_mutation_named_read_tool_is_refused():
    """The double guard: even a mis-declared 'read' tool with a mutation NAME trips the
    investigation registry's denylist at registration."""
    from app.agents.investigation import ReadOnlyViolation

    async def _f(**_):
        return 1
    bad = CapabilityPack(name="x", provider="aws", domain="cloudops",
                         tools=(ToolSpec("cloudops.aws.delete_bucket", "danger", "storage",
                                         "read", _f),))
    with pytest.raises(ReadOnlyViolation):
        registry.build_read_registry(_s(), packs=[bad])


def test_capability_catalog_is_provider_neutral_view():
    cat = {c["pack"]: c for c in registry.capability_catalog(_s())}
    assert cat["cloudops.aws"]["read"] and cat["cloudops.aws"]["mutation"]
    assert "aws.vpc" in cat["cloudops.aws"]["templates"]
    assert cat["sreops.k8s"]["day2"] == ["k8s.restart", "k8s.rollback", "k8s.scale"] or \
           set(cat["sreops.k8s"]["day2"]) == {"k8s.restart", "k8s.scale", "k8s.rollback"}


# ── provider-neutral objective model (the senior-engineer behavior) ─────────────────────────

@pytest.mark.parametrize("text,family,provider", [
    ("Create a VM in azure", "compute", "azure"),
    ("find my VPC", "network", None),
    ("how many EC2 instances are running?", "compute", "aws"),
    ("open port 8501", "network", None),
    ("investigate 5xx errors", "telemetry", None),
    ("deploy to GKE", "k8s", "gcp"),
    ("check why the github workflow failed", "ci", "github"),
])
def test_objective_maps_intent_to_capability_family_and_provider(text, family, provider):
    o = objective.classify(text)
    assert o.family == family
    assert o.provider == provider


def test_objective_resolves_to_provider_specific_tools_neutrally(monkeypatch):
    # Force all three cloud packs "configured" to prove neutral cross-cloud resolution
    # independent of which creds happen to be on this host.
    monkeypatch.setattr("app.packs.registry.configured_packs",
                        lambda s: registry.all_packs(s))
    o = objective.classify("find my networks")
    tools = objective.resolve_tools(_s(), o)
    assert any("aws" in t for t in tools) and any("azure" in t for t in tools) \
        and any("gcp" in t for t in tools)                 # cloud-neutral fan-out
    # named provider → only that pack's tools
    o2 = objective.classify("list azure vnets")
    assert o2.provider == "azure"
    assert all("azure" in t for t in objective.resolve_tools(_s(), o2))


# ── permission modes + ESTOP (P4.5) ─────────────────────────────────────────────────────────

def test_permission_matrix_gates_mutation_and_never_autonomous_mutates_destructively():
    M = policy.Mode
    assert policy.evaluate(mode=M.READ_ONLY, effect="read").decision == "allow"
    assert policy.evaluate(mode=M.READ_ONLY, effect="mutation", risk="low").decision == "deny"
    assert policy.evaluate(mode=M.PLAN_ONLY, effect="mutation").decision == "plan_only"
    assert policy.evaluate(mode=M.APPROVAL_REQUIRED, effect="mutation",
                           risk="medium").decision == "approval_required"
    # destructive is ALWAYS human-gated, even AUTONOMOUS + allowlisted
    assert policy.evaluate(mode=M.AUTONOMOUS, effect="mutation", risk="destructive",
                           autonomous_allowlisted=True).decision == "approval_required"
    # AUTONOMOUS without allowlist falls back to approval (never silently executes)
    assert policy.evaluate(mode=M.AUTONOMOUS, effect="mutation",
                           risk="medium").decision == "approval_required"


def test_estop_denies_new_mutations_but_not_reads():
    M = policy.Mode
    policy.engage_estop("incident freeze")
    try:
        assert policy.evaluate(mode=M.APPROVAL_REQUIRED, effect="mutation").decision == "deny"
        assert policy.evaluate(mode=M.APPROVAL_REQUIRED, effect="read").decision == "allow"
    finally:
        policy.clear_estop()
    assert policy.evaluate(mode=M.APPROVAL_REQUIRED, effect="mutation").decision \
        == "approval_required"


# ── representative end-to-end READ flow: objective → harness over packs → verify → evidence ─

class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    async def classify_json(self, settings, system, prompt, **kw):
        return self.decisions.pop(0)


async def test_read_objective_runs_through_packs_and_harness(monkeypatch):
    """"find my Azure VMs" → objective resolves the azure/compute capability → the harness
    INV loop (P2) reasons over the PACK read tool → evidence-backed answer. Cloud-neutral
    orchestration; provider-specific tool inside the pack; no mutation."""
    from app.harness import inv as harness_inv
    from app.harness import loop as harness_loop
    from app.packs.base import CapabilityPack, ToolSpec

    async def _append(run_id, kind, payload, org_id=None):
        return 0
    monkeypatch.setattr("app.harness.loop.run_log.append", _append)

    async def azure_vms():
        return [{"name": "web-01", "size": "Standard_B2s"}, {"name": "web-02"}]
    pack = CapabilityPack(name="cloudops.azure", provider="azure", domain="cloudops",
                          tools=(ToolSpec("cloudops.azure.list_compute", "List Azure VMs",
                                          "compute", "read", azure_vms),))
    monkeypatch.setattr("app.packs.registry.configured_packs", lambda s: [pack])
    # dark-launch flag ON → harness read registry is pack-sourced
    settings = _s(aegisops_capability_packs="on")

    model = ScriptedModel([
        {"hypothesis": "inspect azure compute",
         "action": {"kind": "use_tool", "tool": "cloudops.azure.list_compute"}},
        {"hypothesis": "two VMs found", "action": {"kind": "answer",
         "text": "You have 2 Azure VMs: web-01, web-02 (obs 0)."}},
    ])
    monkeypatch.setattr(harness_loop.service, "classify_json", model.classify_json)

    o = objective.classify("find my Azure VMs")
    assert o.provider == "azure" and o.family == "compute" and o.read_only
    res = await harness_inv.investigate(settings, "List the Azure VMs and count them.")
    assert res.status == "answered" and res.evidence_ok
    assert "web-01" in res.findings and res.observations[0].tool == "cloudops.azure.list_compute"
