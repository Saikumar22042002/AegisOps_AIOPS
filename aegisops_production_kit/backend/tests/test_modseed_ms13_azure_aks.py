"""MODSEED MS-13 (the last) — azure-aks: Log Analytics + OMS agent, network_policy=calico,
azure_policy_enabled (schema B2 defaults OFF; module defaults observable/governed — which is
what kills the three checkov + two tfsec waivers). Plus the B4 azure.vm→vnet DEP slot: a
known azure.vnet places the VM into its first recorded subnet and the module skips its
dedicated vnet (with `moved` blocks covering the count-index migration).
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.agents import dependency, params
from app.agents.templates import _azure_aks_policy
from app.schemas.workflows import AzureAKSInputs, AzureVMInputs


def _ws(name: str) -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / name
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _tf_env(tmp_path, key: str) -> dict:
    env = dict(os.environ)
    env["TF_DATA_DIR"] = str(tmp_path / f"tfdata-{key}")
    return env


def test_aks_source_invariants():
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws("azure-aks").glob("*.tf")))
    assert re.search(r'variable "enable_monitoring" \{[^}]*default     = true', src, re.S)
    assert re.search(r'variable "network_policy" \{(?:[^{}]|\{[^}]*\})*default     = "calico"', src, re.S)
    assert re.search(r'variable "azure_policy_enabled" \{[^}]*default     = true', src, re.S)
    assert 'resource "azurerm_log_analytics_workspace" "aks"' in src
    assert "log_analytics_workspace_id = azurerm_log_analytics_workspace.aks[\"monitoring\"].id" in src
    assert 'network_plugin = "kubenet"' in src
    assert "role_based_access_control_enabled = true" in src     # the RBAC pin stays
    assert 'version = "~> 3.110"' in src


def test_vm_source_invariants_b4_slot():
    src = "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws("azure-vm").glob("*.tf")))
    # moved blocks migrate the count-index change for existing state (B1)
    assert re.search(r"moved\s*\{\s*from\s*=\s*azurerm_virtual_network\.this\s*"
                     r"to\s*=\s*azurerm_virtual_network\.this\[0\]", src)
    assert re.search(r"moved\s*\{\s*from\s*=\s*azurerm_subnet\.this\s*"
                     r"to\s*=\s*azurerm_subnet\.this\[0\]", src)
    assert "count               = local.use_existing_net ? 0 : 1" in src
    assert "local.use_existing_net ? var.existing_subnet_id : azurerm_subnet.this[0].id" in src


def test_aks_fmt_validate_and_b1_terraform_test(tmp_path):
    d, env = str(_ws("azure-aks")), _tf_env(tmp_path, "aks")
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300, env=env)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    test = subprocess.run(["terraform", "test", "-no-color"], cwd=d,
                          capture_output=True, text=True, timeout=600, env=env)
    assert test.returncode == 0, f"terraform test failed:\n{test.stdout[-2000:]}{test.stderr[-500:]}"
    assert "4 passed, 0 failed" in test.stdout


def test_vm_fmt_validate_and_b1_terraform_test(tmp_path):
    d, env = str(_ws("azure-vm")), _tf_env(tmp_path, "vm")
    fmt = subprocess.run(["terraform", "fmt", "-check", "-diff"], cwd=d,
                         capture_output=True, text=True, timeout=120, env=env)
    assert fmt.returncode == 0, f"fmt -check failed:\n{fmt.stdout}{fmt.stderr}"
    init = subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                          cwd=d, capture_output=True, text=True, timeout=300, env=env)
    assert init.returncode == 0, f"init failed:\n{init.stderr[-800:]}"
    test = subprocess.run(["terraform", "test", "-no-color"], cwd=d,
                          capture_output=True, text=True, timeout=600, env=env)
    assert test.returncode == 0, f"terraform test failed:\n{test.stdout[-2000:]}{test.stderr[-500:]}"
    assert "2 passed, 0 failed" in test.stdout


def test_schemas_b2_defaults():
    aks = AzureAKSInputs(name="c1").model_dump()
    assert aks["enable_monitoring"] is False and aks["network_policy"] == ""
    assert aks["azure_policy_enabled"] is False
    with pytest.raises(Exception):
        AzureAKSInputs(name="c1", network_policy="cilium")
    vm = AzureVMInputs(name="v1", size="Standard_B1s", os="ubuntu-22.04").model_dump()
    assert vm["existing_subnet_id"] == ""


def test_vnet_dep_slot_b4():
    slots = dependency.SLOTS["azure.vm"]
    slot = next(s for s in slots if s.field == "existing_subnet_id")
    assert slot.parent_cloud == "azure" and slot.parent_type == "vnet"
    assert slot.required is False and slot.creator == "azure.vnet"
    assert slot.value_from == "attr:subnet_ids[0]"
    assert "<name>-vnet" in slot.stated_default
    # the RG slot is still there (both coexist)
    assert any(s.field == "resource_group" for s in slots)


def test_card_states_each_addon():
    by = {c["name"]: c for c in _azure_aks_policy(
        {"node_count": 2, "enable_monitoring": True, "network_policy": "calico",
         "azure_policy_enabled": True})}
    assert "Log Analytics" in by["Cluster monitoring"]["detail"]
    assert by["Network policy"]["detail"] == "calico on kubenet"
    assert by["Azure Policy add-on"]["passed"] is True
    by_off = {c["name"]: c for c in _azure_aks_policy({"node_count": 2})}
    assert "Cluster monitoring" not in by_off and "Network policy" not in by_off


def test_params_required_sets_unchanged():
    assert {p.name for p in params.missing_required("azure.aks", {})} == {"name"}
    assert {p.name for p in params.missing_required("azure.vm", {})} == {"name", "size", "os",
                                                                         "allowed_cidr"}


def test_ms13_waivers_are_gone():
    aks_cfg = (_ws("azure-aks") / ".checkov.yaml").read_text(encoding="utf-8")
    for dead in ("CKV_AZURE_4\b", "CKV_AZURE_7\b", "CKV_AZURE_116"):
        assert re.search(dead.replace("\b", r"\b"), aks_cfg) is None, dead
    assert "MS-13" not in aks_cfg
    tfsec_cfg = (_ws("azure-aks") / ".tfsec" / "config.yml").read_text(encoding="utf-8")
    assert "azure-container-logging" not in tfsec_cfg
    assert "azure-container-configured-network-policy" not in tfsec_cfg
    assert "MS-13" not in tfsec_cfg
