"""U1 — real policy checks: predicates over the planned resource attributes (terraform show -json).

A plan with a control DISABLED (encryption off, public access open, weak TLS) must render a real
FAILED check the approver sees — not a fake green pass. Synthetic `resources` (the {type, after}
shape cloudops passes from show_plan) drive the predicates without live Terraform.
"""

from __future__ import annotations

from app.agents import templates


def _find(checks, name):
    return next((c for c in checks if c["name"] == name), None)


def test_ec2_encryption_off_is_a_failed_check():
    resources = [{"type": "aws_instance", "after": {
        "root_block_device": [{"encrypted": False}],
        "metadata_options": [{"http_tokens": "required"}]}}]
    checks = templates.select("aws", "ec2").policy_fn({}, resources)
    enc = _find(checks, "Root volume encrypted")
    assert enc and enc["passed"] is False and enc["evaluated"] is True
    assert _find(checks, "IMDSv2 enforced")["passed"] is True


def test_ec2_hardened_plan_passes():
    resources = [{"type": "aws_instance", "after": {
        "root_block_device": [{"encrypted": True}],
        "metadata_options": [{"http_tokens": "required"}]}}]
    checks = templates.select("aws", "ec2").policy_fn({}, resources)
    assert _find(checks, "Root volume encrypted")["passed"] is True
    assert _find(checks, "IMDSv2 enforced")["passed"] is True


def test_ec2_imdsv1_is_a_failed_check():
    resources = [{"type": "aws_instance", "after": {
        "root_block_device": [{"encrypted": True}],
        "metadata_options": [{"http_tokens": "optional"}]}}]
    checks = templates.select("aws", "ec2").policy_fn({}, resources)
    assert _find(checks, "IMDSv2 enforced")["passed"] is False


def test_s3_public_access_open_is_a_failed_check():
    resources = [
        {"type": "aws_s3_bucket_public_access_block", "after": {
            "block_public_acls": True, "block_public_policy": False,
            "ignore_public_acls": True, "restrict_public_buckets": True}},
        {"type": "aws_s3_bucket_server_side_encryption_configuration", "after": {
            "rule": [{"apply_server_side_encryption_by_default": [{"sse_algorithm": "AES256"}]}]}},
        {"type": "aws_s3_bucket_versioning", "after": {"versioning_configuration": [{"status": "Enabled"}]}},
    ]
    checks = templates.select("aws", "s3").policy_fn({}, resources)
    assert _find(checks, "Public access blocked")["passed"] is False   # one flag OFF
    assert _find(checks, "Server-side encryption")["passed"] is True
    assert _find(checks, "Versioning enabled")["passed"] is True


def test_rds_unencrypted_or_public_fails():
    resources = [{"type": "aws_db_instance", "after": {
        "storage_encrypted": False, "publicly_accessible": True}}]
    checks = templates.select("aws", "rds").policy_fn({"engine": "postgres"}, resources)
    assert _find(checks, "Storage encrypted")["passed"] is False
    assert _find(checks, "Not publicly accessible")["passed"] is False


def test_azure_storage_weak_tls_fails():
    resources = [{"type": "azurerm_storage_account", "after": {
        "min_tls_version": "TLS1_0", "allow_nested_items_to_be_public": True}}]
    checks = templates.select("azure", "storage").policy_fn({}, resources)
    assert _find(checks, "Minimum TLS 1.2")["passed"] is False
    assert _find(checks, "No public blob access")["passed"] is False


def test_no_plan_resources_keeps_checks_not_evaluated():
    # Without a plan (resources=None), plan-derived checks stay honestly "not evaluated".
    checks = templates.select("aws", "ec2").policy_fn({})
    assert all(c["evaluated"] is False and c["passed"] is None for c in checks)
