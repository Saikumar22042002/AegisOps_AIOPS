"""MODSEED MS-10 — aws-ec2: optional SSM Session Manager + CloudWatch agent instance
profile. enable_ssm defaults FALSE at the schema (B2, named verbatim in the spec) and TRUE
at the module (secure/observable bare use — which is what lets the IAM-profile waiver die).
The approval card states "Session Manager access available" when it's on.
B1 proven by the workspace's committed `terraform test` (mock provider + an override for
the default-subnet discovery): old-shape inputs render ZERO IAM/SSM resources.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from app.agents import params
from app.agents.templates import _ec2_policy
from app.schemas.workflows import AWSEC2Inputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "aws-ec2"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _tf_env(tmp_path) -> dict:
    """The live workspace's .terraform carries an A3-injected backend pointer (named state
    workspaces) — an isolated TF_DATA_DIR keeps tests off it entirely."""
    env = dict(os.environ)
    env["TF_DATA_DIR"] = str(tmp_path / "tfdata")
    return env


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_ssm_chain():
    src = _src()
    # the whole chain is for_each-gated on enable_ssm (count would hide it from scanners)
    assert src.count('for_each    = var.enable_ssm ? toset(["ssm"]) : toset([])') == 2  # role+profile
    assert src.count('for_each   = var.enable_ssm ? toset(["ssm"]) : toset([])') == 2   # attachments
    assert "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore" in src
    assert "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy" in src
    assert "iam_instance_profile   = one(values(aws_iam_instance_profile.ssm)[*].name)" in src
    # module default is the secure/observable one; the schema holds the B2 old default
    assert re.search(r'variable "enable_ssm" \{[^}]*default     = true', src, re.S)
    # untouched contracts
    assert 'http_tokens = "required"' in src           # IMDSv2 stays
    assert "encrypted   = true" in src                 # root volume stays encrypted


def test_fmt_validate_and_b1_terraform_test(tmp_path):
    d, env = str(_ws()), _tf_env(tmp_path)
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300, env=env)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"
    test = subprocess.run(["terraform", "test", "-no-color"], cwd=d,
                          capture_output=True, text=True, timeout=600, env=env)
    assert test.returncode == 0, f"terraform test failed:\n{test.stdout[-2000:]}{test.stderr[-500:]}"
    assert "2 passed, 0 failed" in test.stdout


def test_schema_b2_default_off():
    v = AWSEC2Inputs(name="vm1").model_dump()
    assert v["enable_ssm"] is False                    # B2, named verbatim in the spec
    assert AWSEC2Inputs(name="vm1", enable_ssm=True).enable_ssm is True


def test_params_required_set_unchanged():
    missing = {p.name for p in params.missing_required("aws.ec2", {})}
    assert missing == {"name", "instance_type", "os", "key_pair", "allowed_cidr"}


def test_card_states_session_manager_when_on():
    plan = [{"type": "aws_instance",
             "after": {"metadata_options": [{"http_tokens": "required"}],
                       "root_block_device": [{"encrypted": True}]}}]
    by_on = {c["name"]: c for c in _ec2_policy({"enable_ssm": True}, plan)}
    assert by_on["Session Manager access available"]["passed"] is True
    assert "instance profile" in by_on["Session Manager access available"]["detail"]
    assert by_on["IMDSv2 enforced"]["passed"] is True  # existing checks untouched

    by_off = {c["name"]: c for c in _ec2_policy({}, plan)}
    assert "Session Manager access available" not in by_off


def test_iam_profile_waiver_is_gone():
    cfg = (_ws() / ".checkov.yaml").read_text(encoding="utf-8")
    assert "CKV2_AWS_41" not in cfg
    assert "MS-10" not in cfg
