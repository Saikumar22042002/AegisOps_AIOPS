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


def test_select_falls_back_to_generic_module() -> None:
    t = templates.select("aws", "module")
    assert t is not None and t.key == "generic.module"


def test_catalog_covers_three_clouds() -> None:
    clouds = {t["cloud"] for t in templates.catalog()}
    assert {"aws", "azure", "gcp"}.issubset(clouds)


def test_s3_policy_checks() -> None:
    t = templates.select("aws", "s3")
    checks = t.policy_fn({"block_public": True, "versioning": True})
    assert all(c["passed"] for c in checks)


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
