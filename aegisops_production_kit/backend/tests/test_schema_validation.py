"""Per-module Pydantic input validation (6.1) — valid inputs accepted with safe defaults;
invalid inputs rejected (so the agent clarifies the specific field and never plans a bad value).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import workflows as wf


def test_s3_valid_and_defaults():
    ok = wf.AWSS3Inputs(bucket_name="my-data-bucket")
    assert ok.region == "us-east-1" and ok.versioning and ok.block_public


@pytest.mark.parametrize("bad", ["A", "ab", "UPPER-bucket", "has_underscore", "x" * 64])
def test_s3_bucket_name_rejected(bad):
    with pytest.raises(ValidationError):
        wf.AWSS3Inputs(bucket_name=bad)


@pytest.mark.parametrize("itype", ["banana", "t3", "micro", "3.micro", "t3-micro"])
def test_ec2_bad_instance_type_rejected(itype):
    with pytest.raises(ValidationError):
        wf.AWSEC2Inputs(name="x", instance_type=itype, os="ubuntu-22.04")


@pytest.mark.parametrize("itype", ["t3.micro", "t3.large", "m5.large", "c5.2xlarge", "m6i.xlarge"])
def test_ec2_good_instance_type_accepted(itype):
    assert wf.AWSEC2Inputs(name="x", instance_type=itype, os="ubuntu-22.04").instance_type == itype


def test_ec2_bad_os_rejected():
    with pytest.raises(ValidationError):
        wf.AWSEC2Inputs(name="x", instance_type="t3.micro", os="freebsd")


def test_ec2_ports_coerced_from_string_and_list():
    assert wf.AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04",
                           ingress_ports="8501,8502").ingress_ports == [8501, 8502]
    assert wf.AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04",
                           ingress_ports=["80", "443"]).ingress_ports == [80, 443]


def test_ec2_bad_volume_type_rejected():
    with pytest.raises(ValidationError):
        wf.AWSEC2Inputs(name="x", instance_type="t3.micro", os="ubuntu-22.04", root_volume_type="gpX")


@pytest.mark.parametrize("bad", ["AB", "UPPER", "has space", "sym!bol", "x" * 25])
def test_azure_storage_account_name_rejected(bad):
    with pytest.raises(ValidationError):
        wf.AzureStorageInputs(account_name=bad, resource_group="rg")


def test_azure_storage_requires_resource_group():
    with pytest.raises(ValidationError):
        wf.AzureStorageInputs(account_name="mystorage123")


def test_azure_vm_os_choices():
    # Phase 8 / N-05: windows-2022 is now genuinely supported by the module (the platform
    # allows it — screenshot 7); only OSes Azure itself wouldn't create are rejected.
    assert wf.AzureVMInputs(name="vm", os="ubuntu-24.04").os == "ubuntu-24.04"
    assert wf.AzureVMInputs(name="vm", os="windows-2022").os == "windows-2022"
    with pytest.raises(ValidationError):
        wf.AzureVMInputs(name="vm", os="templeos")


def test_azure_postgres_storage_floor():
    with pytest.raises(ValidationError):
        wf.AzurePostgresInputs(name="pg", storage_mb=1024)  # below the 32768 floor
    assert wf.AzurePostgresInputs(name="pg").pg_version == "15"


def test_azure_aks_node_floor():
    with pytest.raises(ValidationError):
        wf.AzureAKSInputs(name="aks", node_count=0)


def test_gcp_gcs_requires_project():
    with pytest.raises(ValidationError):
        wf.GCPGCSInputs(bucket_name="b")
    assert wf.GCPGCSInputs(bucket_name="b", project="p").location == "US"


def test_gcp_defaults():
    assert wf.GCPComputeInputs(name="vm").machine_type == "e2-micro"
    assert wf.GCPCloudSQLInputs(name="db").database_version == "POSTGRES_15"
    assert wf.GCPGKEInputs(name="k").node_count == 2


def test_aws_vpc_az_bounds():
    with pytest.raises(ValidationError):
        wf.AWSVPCInputs(name="net", az_count=0)
    with pytest.raises(ValidationError):
        wf.AWSVPCInputs(name="net", az_count=7)
    assert wf.AWSVPCInputs(name="net").cidr_block == "10.0.0.0/16"


def test_aws_rds_storage_bounds():
    with pytest.raises(ValidationError):
        wf.AWSRDSInputs(identifier="db", allocated_storage=10)  # below 20 GiB floor
    assert wf.AWSRDSInputs(identifier="db").engine == "postgres"


def test_extra_fields_ignored_not_errored():
    # extra="ignore": stray keys from NL extraction don't crash validation.
    ok = wf.AWSS3Inputs(bucket_name="my-bucket", nonsense="x", another=123)
    assert not hasattr(ok, "nonsense")


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 7 / BUG-02 — per-cloud machine shapes: an AWS-style value can never reach a GCP or
# Azure plan (screenshot 12/13: machine_type="ec2-micro" on google_compute_instance).
# ═══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("bad", ["ec2-micro", "t3.micro", "m5.large", "Standard_B1s", "banana"])
def test_gcp_machine_type_rejects_foreign_shapes(bad):
    with pytest.raises(ValidationError):
        wf.GCPComputeInputs(name="vm", machine_type=bad)
    with pytest.raises(ValidationError):
        wf.GCPGKEInputs(name="k", machine_type=bad)


@pytest.mark.parametrize("good", ["e2-micro", "e2-medium", "e2-standard-4", "n2-standard-2",
                                  "c3-highcpu-4", "custom-2-8192", "f1-micro"])
def test_gcp_machine_type_accepts_real_shapes(good):
    assert wf.GCPComputeInputs(name="vm", machine_type=good).machine_type == good


def test_gcp_plan_pipeline_never_sees_ec2_shape():
    # The exact screenshot-13 answer ("test-v1, ec2-micro, ubuntu") must fail validation —
    # which is the same call cloudops makes before ANY terraform plan, so no GCP plan can
    # ever contain an ec2-* machine type.
    from app.agents import params, templates
    t = templates.select("gcp", "vm")
    collected = {"name": "test-v1", "machine_type": "ec2-micro", "os": "ubuntu-22.04", "project": "p"}
    with pytest.raises(ValidationError):
        t.schema(**params.to_tf_vars(t.key, collected))


@pytest.mark.parametrize("bad", ["e2-micro", "t3.micro", "db.t3.medium", "large"])
def test_azure_size_rejects_foreign_shapes(bad):
    with pytest.raises(ValidationError):
        wf.AzureVMInputs(name="vm", size=bad)
    with pytest.raises(ValidationError):
        wf.AzureAKSInputs(name="aks", node_size=bad)


@pytest.mark.parametrize("good", ["Standard_B1s", "Standard_B2s", "Standard_D2s_v5", "Basic_A0"])
def test_azure_size_accepts_real_shapes(good):
    assert wf.AzureVMInputs(name="vm", size=good).size == good


# ═══════════════════════════════════════════════════════════════════════════════════════════
# Phase 8 / N-05 — provider accuracy: the module must accept what the platform actually
# allows (screenshot 6 rejected Windows; screenshot 7 shows the same sandbox creating a
# Windows D-series VM with a default resource group in the portal).
# ═══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("os_", ["ubuntu-22.04", "ubuntu-24.04", "debian-12", "windows-2022"])
def test_azure_vm_accepts_provider_supported_os(os_):
    assert wf.AzureVMInputs(name="vm", os=os_).os == os_


@pytest.mark.parametrize("size", ["Standard_B1s", "Standard_B2s", "Standard_D2s_v5",
                                  "Standard_D4s_v5", "Standard_E2s_v5", "Standard_DS1_v2"])
def test_azure_vm_accepts_bde_series(size):
    assert wf.AzureVMInputs(name="vm", size=size).size == size


def test_azure_vm_still_rejects_genuinely_invalid_os():
    with pytest.raises(ValidationError):
        wf.AzureVMInputs(name="vm", os="freebsd-14")


def test_azure_vm_default_resource_group_semantics():
    # Empty resource_group ⇒ module creates/uses a default one (like the portal) — the schema
    # must accept empty; the module handles create-or-use.
    assert wf.AzureVMInputs(name="vm").resource_group == ""


def test_rds_class_and_cloudsql_tier_shapes():
    with pytest.raises(ValidationError):
        wf.AWSRDSInputs(identifier="db", instance_class="t3.medium")   # missing db. prefix
    assert wf.AWSRDSInputs(identifier="db", instance_class="db.t3.medium").instance_class == "db.t3.medium"
    with pytest.raises(ValidationError):
        wf.GCPCloudSQLInputs(name="sql", tier="f1-micro")              # missing db- prefix
    assert wf.GCPCloudSQLInputs(name="sql", tier="db-f1-micro").tier == "db-f1-micro"
