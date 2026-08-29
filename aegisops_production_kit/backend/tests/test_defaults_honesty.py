"""DEF — silently-defaulted dependency placements are stated explicitly (no invisible placement)."""

from __future__ import annotations

from app.agents.cloudops import _defaulted_dependencies


def test_aws_ec2_default_vpc_is_surfaced():
    d = _defaulted_dependencies("aws", "ec2", {"name": "web"}, [])
    assert d and d[0]["name"] == "VPC / subnet"
    assert "default VPC" in d[0]["note"]


def test_aws_ec2_resolved_subnet_from_plan_is_named():
    resources = [{"type": "aws_instance", "after": {"subnet_id": "subnet-0abc123"}}]
    d = _defaulted_dependencies("aws", "ec2", {"name": "web"}, resources)
    assert d[0]["value"] == "subnet-0abc123"


def test_aws_ec2_user_specified_vpc_not_flagged():
    # The schema's REAL placement input is subnet_id (user-named or DEP-resolved). The old
    # pin keyed on vpc_id — a field no aws.ec2 input carries — so the "don't flag real
    # placements" behavior never actually fired (audit 2026-08-17: the approval card claimed
    # "account's default VPC" while the resolver had bound a named VPC's subnet).
    d = _defaulted_dependencies("aws", "ec2", {"name": "web", "subnet_id": "subnet-user"}, [])
    assert d == []


def test_gcp_vm_default_network_surfaced():
    d = _defaulted_dependencies("gcp", "vm", {"name": "gce"}, [])
    assert d and d[0]["name"] == "Network" and d[0]["value"] == "default"


def test_azure_vm_auto_created_rg_surfaced():
    d = _defaulted_dependencies("azure", "vm", {"name": "winbox"}, [])
    assert d and d[0]["name"] == "Resource group" and "winbox-rg" in d[0]["value"]


def test_azure_vm_user_rg_not_flagged():
    d = _defaulted_dependencies("azure", "vm", {"name": "x", "resource_group": "prod-rg"}, [])
    assert d == []
