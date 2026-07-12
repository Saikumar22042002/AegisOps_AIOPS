"""Parameter-collection coverage (6.1) — every real module asks for exactly its decision-critical
params, defaults the rest, and transforms collected values to Terraform variables correctly.
"""

from __future__ import annotations

import pytest

from app.agents import params, templates

# template key → the EXACT set of params that must be asked (required, no safe default).
# Phase 8 / N-02: VM modules also ask for the allowed source CIDR (default-closed is a
# decision the USER makes, so it must be asked).
_REQUIRED = {
    "aws.ec2": {"name", "instance_type", "os", "key_pair", "allowed_cidr"},
    "aws.s3": {"bucket_name"},
    "aws.rds": {"identifier"},
    "aws.vpc": {"name"},
    "aws.eks": {"cluster_name", "vpc_id", "subnet_ids"},
    "azure.resource_group": {"name"},
    "azure.storage": {"account_name", "resource_group"},
    "azure.vm": {"name", "size", "os", "allowed_cidr"},
    "azure.db": {"name"},  # MS-8 (B4): key renamed from azure.postgres; required set unchanged
    "azure.aks": {"name"},
    "gcp.gcs": {"bucket_name"},
    "gcp.vm": {"name", "machine_type", "os", "allowed_cidr"},
    "gcp.gke": {"name"},
    "gcp.cloudsql": {"name"},
}


def test_every_registered_template_has_params():
    # Each curated template must declare a ParamSpec list (no module ships un-collectable).
    for t in templates.TEMPLATES:
        assert params.specs_for(t.key), f"{t.key} has no ParamSpec declarations"


@pytest.mark.parametrize("key,expected", _REQUIRED.items())
def test_asks_only_decision_critical_params(key, expected):
    missing = {p.name for p in params.missing_required(key, {})}
    assert missing == expected, f"{key} should ask exactly {expected}, got {missing}"


def test_ec2_never_asks_for_defaulted_network_params():
    asked = {p.name for p in params.missing_required("aws.ec2", {})}
    for defaulted in ("region", "vpc_id", "subnet_id", "root_volume_size", "root_volume_type"):
        assert defaulted not in asked


@pytest.mark.parametrize("key,expected", _REQUIRED.items())
def test_nothing_missing_once_required_supplied(key, expected):
    collected = {name: _sample(name) for name in expected}
    assert params.missing_required(key, collected) == []


def test_request_payload_only_lists_missing():
    collected = {"name": "web-01", "instance_type": "t3.micro"}  # 2 of 5 supplied
    payload = params.request_payload("aws.ec2", collected)
    names = {i["name"] for i in payload["items"]}
    assert names == {"os", "key_pair", "allowed_cidr"}
    assert payload["collected"] == collected
    assert "**Operating system**" in params.summary_text("aws.ec2", collected)


def test_ec2_key_pair_transform_create():
    out = params.to_tf_vars("aws.ec2", {"name": "web", "key_pair": "create"})
    assert out["create_key_pair"] is True
    assert out["key_name"] == "web-key"  # generated from the instance name, always a real value
    assert "key_pair" not in out


def test_ec2_key_pair_transform_existing():
    out = params.to_tf_vars("aws.ec2", {"name": "web", "key_pair": "my-existing-key"})
    assert out["create_key_pair"] is False
    assert out["key_name"] == "my-existing-key"


def test_non_ec2_transform_is_identity():
    collected = {"bucket_name": "logs-prod", "region": "us-west-2"}
    assert params.to_tf_vars("aws.s3", collected) == collected


def test_extraction_fields_lists_choices():
    fields = params.extraction_fields("aws.ec2")
    assert "os (one of:" in fields and "amazon-linux-2023" in fields


def _sample(name: str):
    return {
        "subnet_ids": ["subnet-a", "subnet-b"],
        "os": "ubuntu-22.04",
        "instance_type": "t3.micro",
        "machine_type": "e2-micro",
        "size": "Standard_B1s",
        "vpc_id": "vpc-123",
        "account_name": "mystorage123",
        "resource_group": "rg-app",
        "allowed_cidr": "203.0.113.7/32",
    }.get(name, f"val-{name}")
