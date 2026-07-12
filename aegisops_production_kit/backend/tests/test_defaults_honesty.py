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
    d = _defaulted_dependencies("aws", "ec2", {"name": "web", "vpc_id": "vpc-user"}, [])
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
