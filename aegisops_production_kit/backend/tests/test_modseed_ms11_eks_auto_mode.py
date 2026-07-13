"""MODSEED MS-11 — eks-provision: eks_mode = standard | auto.
standard = the pre-enhancement managed-node-group path byte-for-byte (B2 default at BOTH
levels, verbatim per spec: "eks_mode=standard"); auto = EKS Auto Mode (API authentication,
compute_config with the general-purpose pool — the registry module wires the elastic-LB +
block-storage configs and attaches the auto-mode IAM policy set). The card states the mode.
B1 proven by the committed `terraform test`: the registry module is override_module-mocked
(its internals are upstream's tested product) and OUR conditional wiring is asserted
through the root locals.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.agents import params
from app.agents.templates import _eks_policy
from app.schemas.workflows import AWSEKSInputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "eks-provision"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _tf_env(tmp_path) -> dict:
    env = dict(os.environ)
    env["TF_DATA_DIR"] = str(tmp_path / "tfdata")
    return env


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_mode_wiring():
    src = _src()
    assert 'contains(["standard", "auto"], var.eks_mode)' in src
    assert re.search(r'variable "eks_mode" \{(?:[^{}]|\{[^}]*\})*default     = "standard"', src, re.S)
    # auto wires API auth + the general-purpose pool; standard leaves nulls (module defaults)
    assert 'authentication_mode = local.auto_mode ? "API" : null' in src
    assert 'node_pools = ["general-purpose"]' in src
    assert "bootstrap_self_managed_addons = local.auto_mode ? false : null" in src
    # the pre-enhancement node group survives verbatim on the standard path
    assert "desired_size   = var.desired_size" in src
    # hardening + pins untouched
    assert "cluster_endpoint_public_access  = false" in src
    assert 'version = "~> 5.60"' in src and 'version = "~> 20.8"' in src


def test_fmt_validate_and_b1_terraform_test(tmp_path):
    d, env = str(_ws()), _tf_env(tmp_path)
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=600, env=env)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    val = subprocess.run(["terraform", "validate", "-no-color"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert val.returncode == 0, f"validate failed:\n{val.stdout}{val.stderr}"
    test = subprocess.run(["terraform", "test", "-no-color"], cwd=d,
                          capture_output=True, text=True, timeout=600, env=env)
    assert test.returncode == 0, f"terraform test failed:\n{test.stdout[-2000:]}{test.stderr[-500:]}"
    assert "2 passed, 0 failed" in test.stdout


def test_schema_b2_default_standard():
    v = AWSEKSInputs(cluster_name="c1", vpc_id="vpc-1", subnet_ids=["s-1", "s-2"]).model_dump()
    assert v["eks_mode"] == "standard"                  # B2, verbatim per spec
    ok = AWSEKSInputs(cluster_name="c1", vpc_id="vpc-1", subnet_ids=["s-1"], eks_mode="auto")
    assert ok.eks_mode == "auto"
    with pytest.raises(Exception):
        AWSEKSInputs(cluster_name="c1", vpc_id="vpc-1", subnet_ids=["s-1"], eks_mode="fargate")


def test_params_required_set_unchanged():
    missing = {p.name for p in params.missing_required("aws.eks", {})}
    assert missing == {"cluster_name", "vpc_id", "subnet_ids"}


def test_card_states_the_mode():
    by_auto = {c["name"]: c for c in _eks_policy({"eks_mode": "auto"})}
    assert by_auto["Cluster mode"]["passed"] is True
    assert "Auto Mode" in by_auto["Cluster mode"]["detail"]
    by_std = {c["name"]: c for c in _eks_policy({})}
    assert "managed node groups" in by_std["Cluster mode"]["detail"]
