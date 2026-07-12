"""MODSEED MS-7 — aws-rds enhanced: multi-engine + latest-version data source + mandatory-CIDR
SG + subnet group + engine-aware ports/log-exports + sensitive connection string.
BACKCOMPAT gates: B1 (a REAL terraform plan from old-shape stored inputs renders the exact old
shape) and B2 (schema defaults preserve old behavior; the module's own defaults are secure).
Owner-binding first proof: the rds log-export waivers are GONE from the scanner config.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from app.agents import params, templates
from app.agents.templates import _rds_policy
from app.schemas.workflows import AWSRDSInputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "aws-rds"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_multi_engine_and_backcompat():
    src = _src()
    # multi-engine with engine-aware ports/exports/DSN scheme
    assert 'contains(["postgres", "mysql", "mariadb"], var.engine)' in src
    assert "postgres = 5432" in src and "mysql    = 3306" in src and "mariadb  = 3306" in src
    assert '["postgresql", "upgrade"]' in src and '["error", "general", "slowquery"]' in src
    # CORRECT provider attribute (the plural form — a typo here silently disables exports)
    assert "enabled_cloudwatch_logs_exports" in src
    # latest-engine-version data source, resolved only when needed
    assert 'data "aws_rds_engine_version" "selected"' in src
    assert "local.want_latest || var.enable_log_exports ? 1 : 0" in src
    # mandatory-CIDR SG: no world-open literal anywhere, validation rejects /0
    assert "0.0.0.0/0" not in src
    assert 'endswith(var.allowed_cidr, "/0")' in src
    # conditional new capability (B1): SG + subnet group + logging param group all gated
    assert 'create_sg           = var.allowed_cidr != ""' in src
    assert "create_subnet_group = length(var.subnet_ids) > 0" in src
    assert 'for_each    = var.enable_log_exports ? toset(["logging"]) : toset([])' in src
    # kept contracts
    assert "manage_master_user_password     = true" in src
    assert 'version = "~> 5.60"' in src
    assert 'backend "pg"' not in src
    # sensitive connection string without credentials
    assert 'output "connection_string"' in src and "sensitive   = true" in src
    # no password attribute/variable anywhere — RDS manages the master password
    assert re.search(r"^\s*password\s*=", src, re.MULTILINE) is None
    assert 'variable "password"' not in src and "master_password " not in src


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


def test_b1_noop_replan_gate_real_terraform_plan():
    """B1 (binding): pre-enhancement STORED inputs, schema-validated (so every new field is
    passed explicitly at its B2 old-behavior default), REAL `terraform plan` → the module
    renders the EXACT old shape: one aws_db_instance, no SG / subnet group / parameter
    group / exports / engine_version pin. Against a real pre-enhancement resource this is
    a zero-change re-plan, because every rendered attribute equals the old rendering.
    Runs without cloud creds via a TEST-ONLY *_override.tf (removed in finally)."""
    d = _ws()
    override = d / "zz_b1_test_override.tf"
    plan_out = d / "zz_b1_test.plan"
    stored_old_inputs = {"identifier": "b1-gate", "engine": "postgres",
                         "instance_class": "db.t3.medium", "allocated_storage": 20}
    validated = AWSRDSInputs(**stored_old_inputs).model_dump()
    assert validated["enable_log_exports"] is False        # B2 at the schema level
    assert validated["allowed_cidr"] == "" and validated["subnet_ids"] == []
    assert validated["engine_version"] == ""

    override.write_text(
        '# TEST-ONLY (B1 gate) - written and removed by test_modseed_ms7_aws_rds.py\n'
        'provider "aws" {\n'
        '  region                      = "us-east-1"\n'
        '  access_key                  = "fake"\n'
        '  secret_key                  = "fake"\n'
        '  skip_credentials_validation = true\n'
        '  skip_requesting_account_id  = true\n'
        '  skip_metadata_api_check     = true\n'
        '}\n', encoding="utf-8")
    try:
        subprocess.run(["terraform", "init", "-reconfigure", "-input=false", "-no-color"],
                       cwd=str(d), capture_output=True, text=True, timeout=300, check=True)
        var_args = []
        for k, v in validated.items():
            var_args += ["-var", f"{k}={json.dumps(v) if isinstance(v, (list, bool)) else v}"]
        plan = subprocess.run(["terraform", "plan", "-refresh=false", "-input=false",
                               "-no-color", f"-out={plan_out.name}", *var_args],
                              cwd=str(d), capture_output=True, text=True, timeout=300)
        assert plan.returncode == 0, f"plan failed:\n{plan.stdout[-1200:]}{plan.stderr[-800:]}"
        show = subprocess.run(["terraform", "show", "-json", plan_out.name], cwd=str(d),
                              capture_output=True, text=True, timeout=120)
        pj = json.loads(show.stdout)
        changes = {r["address"]: r["change"]["actions"] for r in pj.get("resource_changes", [])}
        assert changes == {"aws_db_instance.this": ["create"]}, changes
        after = next(r for r in pj["resource_changes"]
                     if r["address"] == "aws_db_instance.this")["change"]["after"]
        assert after.get("enabled_cloudwatch_logs_exports") is None
        assert after.get("parameter_group_name") is None
        assert after.get("vpc_security_group_ids") is None
        assert after.get("db_subnet_group_name") is None
        assert after.get("engine_version") is None
        assert after.get("port") == 5432                    # explicit now, equals the old default
        # the old contract, attribute for attribute
        assert after["engine"] == "postgres" and after["instance_class"] == "db.t3.medium"
        assert after["allocated_storage"] == 20 and after["username"] == "aegisadmin"
        assert after["manage_master_user_password"] is True and after["storage_encrypted"] is True
        assert after["skip_final_snapshot"] is True and after["publicly_accessible"] is False
    finally:
        override.unlink(missing_ok=True)
        plan_out.unlink(missing_ok=True)


def test_schema_backcompat_defaults_and_new_bounds():
    v = AWSRDSInputs(identifier="db1").model_dump()
    assert v["engine"] == "postgres" and v["engine_version"] == ""
    assert v["allowed_cidr"] == "" and v["subnet_ids"] == []
    assert v["enable_log_exports"] is False                 # B2
    with pytest.raises(Exception):
        AWSRDSInputs(identifier="db1", engine="oracle")
    with pytest.raises(Exception):
        AWSRDSInputs(identifier="db1", allowed_cidr="0.0.0.0/0")
    with pytest.raises(Exception):
        AWSRDSInputs(identifier="db1", allowed_cidr="10.0.0.0/0")
    with pytest.raises(Exception):
        AWSRDSInputs(identifier="db1", allowed_cidr="not-a-cidr")
    ok = AWSRDSInputs(identifier="db1", engine="mariadb", allowed_cidr="10.20.0.0/16",
                      enable_log_exports=True)
    assert ok.engine == "mariadb" and ok.allowed_cidr == "10.20.0.0/16"


def test_params_still_ask_only_the_identifier():
    assert {p.name for p in params.missing_required("aws.rds", {})} == {"identifier"}


def test_policy_blocks_world_open_db_sg_and_states_exports():
    plan_resources = [
        {"type": "aws_db_instance",
         "after": {"storage_encrypted": True, "publicly_accessible": False}},
        {"type": "aws_security_group",
         "after": {"ingress": [{"cidr_blocks": ["10.0.0.0/16"], "from_port": 3306}]}},
    ]
    by = {c["name"]: c for c in _rds_policy({"engine": "mysql", "enable_log_exports": True},
                                            plan_resources)}
    assert by["DB security group scoped (no /0)"]["passed"] is True
    assert by["Engine-aware log exports"]["passed"] is True
    assert "mysql" in by["Engine-aware log exports"]["detail"]

    world_open = [
        {"type": "aws_db_instance",
         "after": {"storage_encrypted": True, "publicly_accessible": False}},
        {"type": "aws_security_group",
         "after": {"ingress": [{"cidr_blocks": ["0.0.0.0/0"], "from_port": 5432}]}},
    ]
    by_bad = {c["name"]: c for c in _rds_policy({"engine": "postgres"}, world_open)}
    assert by_bad["DB security group scoped (no /0)"]["passed"] is False
    # pre-plan fallback judges the input CIDR
    by_in = {c["name"]: c for c in _rds_policy({"allowed_cidr": "0.0.0.0/0"}, None)}
    assert by_in["DB security group scoped (no /0)"]["passed"] is False


def test_first_proof_log_export_waivers_are_gone():
    """Owner-binding: MS-7's first proof — the rds log-export waivers no longer exist,
    and nothing in either config leans on MS-7 anymore (the guard enforces this globally;
    this pins the first proof by name)."""
    cfg = (_ws() / ".checkov.yaml").read_text(encoding="utf-8")
    assert "CKV_AWS_129" not in cfg
    assert "CKV2_AWS_30" not in cfg
    assert "MS-7" not in cfg
    tfsec_cfg = (_ws() / ".tfsec" / "config.yml").read_text(encoding="utf-8")
    assert "MS-7" not in tfsec_cfg


def test_template_registration_unchanged_and_synonyms():
    t = templates.by_key("aws.rds")
    assert t is not None and t.workspace == "aws-rds"        # B3: dir name immutable
    for syn in ("database", "db", "postgres", "mysql", "sql"):
        assert templates.select("aws", syn) is t, syn
