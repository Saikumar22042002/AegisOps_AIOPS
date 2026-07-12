"""MODSEED MS-9 — gcp-cloudsql enhanced: private-VPC-peering, backup/PITR, maintenance
window, query insights, ssl_mode, deletion_protection var, optional CMEK with a DEP slot
on gcp.kms (offered when a ring exists, never forced). B1 proven by the workspace's
committed `terraform test` (mock providers): old-shape stored inputs — INCLUDING the
legacy world-open 'all' authorized network with its historical name — render the exact
pre-enhancement plan.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from app.agents import dependency, params, templates
from app.agents.templates import _gcp_cloudsql_policy
from app.schemas.workflows import GCPCloudSQLInputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "gcp-cloudsql"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_options_and_secure_defaults():
    src = _src()
    # module defaults are the SECURE ones (scanners evaluate them)…
    assert re.search(r'variable "backup_enabled" \{[^}]*default     = true', src, re.S)
    assert re.search(r'variable "ssl_mode" \{(?:[^{}]|\{[^}]*\})*ENCRYPTED_ONLY', src, re.S)
    assert re.search(r'variable "authorized_networks" \{[^}]*default     = \[\]', src, re.S)
    # …with the full 10-flag observability set as the module-default database_flags
    for flag in ("log_checkpoints", "log_connections", "log_disconnections", "log_lock_waits",
                 "log_temp_files", "log_hostname", "log_min_messages", "log_statement",
                 "log_duration", "cloudsql.enable_pgaudit"):
        assert flag in src, flag
    # the legacy world-open entry keeps its historical name (B1: no in-place rename)
    assert 'cidr == "0.0.0.0/0" ? "all" : "net-${idx}"' in src
    # private peering drops the public path; CMEK and deletion protection are var-driven
    assert "ipv4_enabled    = !local.private" in src
    assert 'encryption_key_name = var.encryption_key_name != "" ? var.encryption_key_name : null' in src
    assert "deletion_protection = var.deletion_protection" in src
    # generated root password KEPT; pins unchanged
    assert 'resource "random_password" "root"' in src
    assert 'version = "~> 5.40"' in src and 'version = "~> 3.6"' in src
    assert 'backend "pg"' not in src


def test_fmt_init_validate_real_terraform():
    d = str(_ws())
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-reconfigure", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"


def test_b1_gate_via_native_terraform_test_with_mock_providers():
    res = subprocess.run(["terraform", "test", "-no-color"], cwd=str(_ws()),
                         capture_output=True, text=True, timeout=600)
    assert res.returncode == 0, f"terraform test failed:\n{res.stdout[-2000:]}{res.stderr[-500:]}"
    assert "5 passed, 0 failed" in res.stdout


def test_schema_backcompat_defaults_and_bounds():
    v = GCPCloudSQLInputs(name="db1").model_dump()
    assert v["authorized_networks"] == ["0.0.0.0/0"]        # the legacy default, preserved
    assert v["backup_enabled"] is False and v["database_flags"] == {}
    assert v["ssl_mode"] == "" and v["private_network"] == ""
    assert v["enable_query_insights"] is False and v["maintenance_day"] == 0
    assert v["deletion_protection"] is False and v["encryption_key_name"] == ""
    with pytest.raises(Exception):
        GCPCloudSQLInputs(name="db1", ssl_mode="REQUIRE_SSL")     # not a real mode
    with pytest.raises(Exception):
        GCPCloudSQLInputs(name="db1", maintenance_day=8)
    ok = GCPCloudSQLInputs(name="db1", private_network="projects/p/global/networks/n",
                           ssl_mode="ENCRYPTED_ONLY", backup_enabled=True)
    assert ok.ssl_mode == "ENCRYPTED_ONLY"


def test_params_ask_only_the_name():
    assert {p.name for p in params.missing_required("gcp.cloudsql", {})} == {"name"}


def test_cmek_dep_slot_offered_never_forced():
    slots = dependency.SLOTS["gcp.cloudsql"]
    slot = next(s for s in slots if s.field == "encryption_key_name")
    assert slot.parent_cloud == "gcp" and slot.parent_type == "kms"
    assert slot.required is False                            # never forced
    assert slot.value_from == "attr:key_ids[0]"              # the ring's first key id
    assert slot.stated_default and "Google-managed" in slot.stated_default


def test_policy_world_open_fails_visibly_and_options_stated():
    # the legacy default is world-open — the approver must SEE that fail
    by = {c["name"]: c for c in _gcp_cloudsql_policy(
        {"database_version": "POSTGRES_15", "authorized_networks": ["0.0.0.0/0"]})}
    assert by["No world-open authorized networks"]["passed"] is False
    assert "0.0.0.0/0" in by["No world-open authorized networks"]["detail"]

    by_ok = {c["name"]: c for c in _gcp_cloudsql_policy(
        {"database_version": "POSTGRES_15", "authorized_networks": ["10.0.0.0/16"],
         "backup_enabled": True, "encryption_key_name": "projects/p/…/cryptoKeys/k"})}
    assert by_ok["No world-open authorized networks"]["passed"] is True
    assert by_ok["Automated backups + PITR"]["passed"] is True
    assert by_ok["CMEK encryption"]["passed"] is True

    by_priv = {c["name"]: c for c in _gcp_cloudsql_policy(
        {"database_version": "POSTGRES_15", "private_network": "projects/p/global/networks/n"})}
    assert by_priv["Network exposure"]["passed"] is True
    assert "No world-open authorized networks" not in by_priv


def test_waivers_removed_and_no_ms9_references():
    cfg = (_ws() / ".checkov.yaml").read_text(encoding="utf-8")
    # backups + world-open waivers are DEAD (word-boundary: CKV_GCP_110/111 still exist)
    assert re.search(r"CKV_GCP_14\b", cfg) is None
    assert re.search(r"CKV_GCP_11\b", cfg) is None
    assert "MS-9" not in cfg
    tfsec_cfg = (_ws() / ".tfsec" / "config.yml").read_text(encoding="utf-8")
    for dead in ("google-sql-enable-backup", "google-sql-pg-log-connections",
                 "google-sql-pg-log-checkpoints", "google-sql-pg-log-disconnections",
                 "google-sql-pg-log-lock-waits", "google-sql-enable-pg-temp-file-logging"):
        assert dead not in tfsec_cfg, dead
    assert "MS-9" not in tfsec_cfg
