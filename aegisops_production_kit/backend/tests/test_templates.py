"""Unit tests for the multi-cloud workflow template registry + input schemas."""

from __future__ import annotations

import pytest

from app.agents import templates
from app.schemas.workflows import AWSS3Inputs, parse_freeform


def test_select_multicloud() -> None:
    assert templates.select("aws", "s3").key == "aws.s3"
    assert templates.select("aws", "eks").key == "aws.eks"
    assert templates.select("azure", "storage").key == "azure.storage"
    assert templates.select("gcp", "gcs").key == "gcp.gcs"


def test_select_is_cloud_safe() -> None:
    # No cross-cloud fallback: an Azure/GCP "ec2" request must NEVER resolve to the AWS module.
    # (Phase 5: "ec2" on azure/gcp is a synonym for that cloud's VM, so it resolves to
    # azure.vm / gcp.vm — still the requested cloud, never aws.ec2.)
    assert templates.select("azure", "ec2").key == "azure.vm"
    assert templates.select("gcp", "ec2").key == "gcp.vm"
    assert templates.select("azure", "ec2").cloud == "azure"
    assert templates.select("gcp", "ec2").cloud == "gcp"
    # No runtime/generic module escape hatch (2.3): unknown resource → None (agent clarifies).
    assert templates.select("aws", "module") is None
    # Genuinely unsupported cloud/resource combos → None, never a wrong-cloud plan.
    assert templates.select("aws", "storage") is None      # "storage" is Azure-branded
    assert templates.select("gcp", "resource_group") is None
    assert templates.select("kubernetes", "vm") is None     # no k8s provisioning templates


def test_catalog_covers_three_clouds() -> None:
    clouds = {t["cloud"] for t in templates.catalog()}
    assert {"aws", "azure", "gcp"}.issubset(clouds)


def test_s3_policy_checks() -> None:
    # P8 honesty: genuinely-evaluated checks carry a real pass/fail; controls the module
    # enforces but the policy engine doesn't yet VERIFY are marked evaluated=False / passed=None
    # ("not evaluated"), never a green pass. Real predicates over the plan land in Phase 2 (U1).
    t = templates.select("aws", "s3")
    checks = t.policy_fn({"block_public": True, "versioning": True})
    evaluated = [c for c in checks if c.get("evaluated") is not False]
    not_evaluated = [c for c in checks if c.get("evaluated") is False]
    assert evaluated and all(c["passed"] is True for c in evaluated)   # block_public + versioning
    assert not_evaluated and all(c["passed"] is None for c in not_evaluated)  # SSE, module version
    # a failing real predicate surfaces as a real fail (not hidden)
    bad = t.policy_fn({"block_public": False, "versioning": True})
    assert any(c["name"] == "Public access blocked" and c["passed"] is False for c in bad)


def test_policy_checks_never_fake_a_pass() -> None:
    """Across every template, a check is either a real evaluated predicate (bool passed) or an
    explicit not-evaluated placeholder (passed is None) — never a hardcoded True pretending to
    be verified (P8 honesty)."""
    for entry in templates.catalog():
        t = templates.select(entry["cloud"], entry["resource"])
        for c in t.policy_fn({}):
            if c.get("evaluated") is False:
                assert c["passed"] is None, f"{t.key}:{c['name']} not-evaluated must have passed=None"
            else:
                assert isinstance(c["passed"], bool), f"{t.key}:{c['name']} evaluated must be a real bool"


def test_s3_input_validation() -> None:
    ok = AWSS3Inputs(bucket_name="my-data-bucket", region="us-east-1")
    assert ok.versioning is True and ok.block_public is True
    with pytest.raises(Exception):
        AWSS3Inputs(bucket_name="A")  # invalid: uppercase + too short


def test_parse_freeform() -> None:
    d = parse_freeform("bucket_name=logs-prod, region=us-west-2")
    assert d["bucket_name"] == "logs-prod"
    assert d["region"] == "us-west-2"
    d2 = parse_freeform("subnet_ids=a,b,c")
    assert d2["subnet_ids"] == ["a", "b", "c"]
