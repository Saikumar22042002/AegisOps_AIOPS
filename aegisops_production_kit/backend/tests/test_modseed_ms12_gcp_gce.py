"""MODSEED MS-12 — gcp-gce: shielded VM, OS Login, preemptible/spot (maintenance
implications stated on the card), optional least-scope service account, and the B4
gcp.vm→network DEP slot (a known gcp.vpc places the VM — BY DESIGN; the DEF default-network
row now only appears for the default placement, updated deliberately and recorded).
KEPT: generated SSH key + one-time reveal. B1 proven by the committed `terraform test`.
The enhanced module needs NO scanner waivers at all — both config files were DELETED
(19/0 checkov, tfsec clean, bare)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from app.agents import dependency, params
from app.agents.cloudops import _defaulted_dependencies
from app.agents.templates import _gcp_gce_policy
from app.schemas.workflows import GCPComputeInputs


def _ws() -> Path:
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand / "gcp-gce"
    raise FileNotFoundError("infra/terraform-workspaces not found")


def _tf_env(tmp_path) -> dict:
    env = dict(os.environ)
    env["TF_DATA_DIR"] = str(tmp_path / "tfdata")
    return env


def _src() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(_ws().glob("*.tf")))


def test_source_invariants_options_and_kept_contracts():
    src = _src()
    # module defaults are the secure ones (shielded on, keys blocked, no public IP)
    assert re.search(r'variable "enable_shielded" \{[^}]*default     = true', src, re.S)
    assert re.search(r'variable "block_project_ssh_keys" \{[^}]*default     = true', src, re.S)
    assert re.search(r'variable "public_ip" \{[^}]*default     = false', src, re.S)
    # options render honestly
    assert "enable_secure_boot          = true" in src and "enable_vtpm" in src
    assert 'provisioning_model          = "SPOT"' in src and "automatic_restart           = false" in src
    assert '{ enable-oslogin = "TRUE" }' in src
    assert 'scopes = ["logging-write", "monitoring-write"]' in src   # least scope
    # the network var drives the instance AND both firewalls (B4 slot target)
    assert src.count("network = var.network") == 3
    # KEPT: generated SSH key + one-time reveal; pins unchanged
    assert 'resource "tls_private_key" "ssh"' in src
    assert re.search(r'output "private_key_pem" \{[^}]*sensitive = true', src, re.S)
    assert 'version = "~> 5.40"' in src and 'version = "~> 4.0"' in src


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
    assert "5 passed, 0 failed" in test.stdout


def test_schema_b2_old_defaults():
    v = GCPComputeInputs(name="vm1").model_dump()
    assert v["network"] == "default" and v["public_ip"] is True     # the OLD behavior
    assert v["enable_shielded"] is False and v["block_project_ssh_keys"] is False
    assert v["enable_oslogin"] is False and v["spot"] is False
    assert v["service_account_email"] == ""


def test_network_dep_slot_b4():
    slots = dependency.SLOTS["gcp.vm"]
    slot = next(s for s in slots if s.field == "network")
    assert slot.parent_cloud == "gcp" and slot.parent_type == "vpc"
    assert slot.required is False and slot.creator == "gcp.vpc"
    assert slot.value_from == "name" and slot.wires == {"network": "input:name"}
    assert "default network" in slot.stated_default


def test_def_default_network_row_is_now_conditional_b4():
    """B4 deliberate change (recorded): the DEF row only flags the DEFAULT placement —
    a slot-filled or user-named network is a real placement with its own provenance."""
    d = _defaulted_dependencies("gcp", "vm", {"name": "gce"}, [])
    assert d and d[0]["name"] == "Network" and d[0]["value"] == "default"
    assert _defaulted_dependencies("gcp", "vm", {"name": "gce", "network": "prod-network"}, []) == []


def test_card_states_each_option():
    by = {c["name"]: c for c in _gcp_gce_policy(
        {"enable_shielded": True, "spot": True, "enable_oslogin": True,
         "service_account_email": "least@p.iam.gserviceaccount.com"})}
    assert by["Shielded VM"]["passed"] is True
    assert "STOPPED by GCP at any time" in by["Spot/preemptible instance"]["detail"]
    assert "no automatic restart" in by["Spot/preemptible instance"]["detail"]
    assert "generated key is unused" in by["OS Login"]["detail"]
    assert "least scope" in by["Dedicated service account (least scope)"]["name"].lower()
    by_off = {c["name"]: c for c in _gcp_gce_policy({})}
    assert "Shielded VM" not in by_off and "Spot/preemptible instance" not in by_off


def test_params_required_set_unchanged():
    missing = {p.name for p in params.missing_required("gcp.vm", {})}
    assert missing == {"name", "machine_type", "os", "allowed_cidr"}


def test_all_scanner_waivers_are_gone():
    """The enhanced module is clean bare — no .checkov.yaml, no .tfsec config at all."""
    assert not (_ws() / ".checkov.yaml").exists()
    assert not (_ws() / ".tfsec" / "config.yml").exists()
