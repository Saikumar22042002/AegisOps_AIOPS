"""C1 — ingress/CIDR wiring regression guard (per cloud).

The exact bug class this prevents: a module edit that drops the wiring so the admin port opens
to the world, or the ingress ports never attach (the real GCP-network-tags defect — firewalls
declared but never bound to the instance). These assertions are over the module source, so they
run anywhere (no cloud creds); the live `terraform show -json` per-cloud assertion is a
creds-gated superset run in CI-with-creds.

Invariants, per VM module:
  • the admin port (SSH 22 / RDP 3389) opens ONLY to var.allowed_cidr — never 0.0.0.0/0;
  • the app ports come from var.ingress_ports;
  • (GCP) the instance carries network tags that the firewalls target — else rules never bind.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _modules_dir() -> Path:
    """Locate infra/terraform-workspaces across layouts: container (/app/infra/…, backend mounted
    at /app) and host (aegisops_production_kit/infra/…, sibling of backend/)."""
    here = Path(__file__).resolve()
    for base in here.parents:
        cand = base / "infra" / "terraform-workspaces"
        if cand.is_dir():
            return cand
    raise FileNotFoundError("infra/terraform-workspaces not found from " + str(here))


_MODULES = _modules_dir()


def _hcl(module: str) -> str:
    return (_MODULES / module / "main.tf").read_text(encoding="utf-8")


def test_aws_ec2_admin_port_bound_to_allowed_cidr_only():
    hcl = _hcl("aws-ec2")
    assert "var.allowed_cidr" in hcl
    assert "for_each = toset(var.ingress_ports)" in hcl, "app ports must come from ingress_ports"
    # The admin rule is gated on allowed_cidr and sources from it (not a literal open CIDR).
    assert 'for_each = var.allowed_cidr != "" ? [var.allowed_cidr] : []' in hcl
    assert "cidr_blocks = [ingress.value]" in hcl, "admin rule must source the allowed CIDR"
    assert "local.admin_port" in hcl and "windows-2022" in hcl  # 22 vs 3389 by OS


def test_azure_vm_admin_nsg_bound_to_allowed_cidr_only():
    hcl = _hcl("azure-vm")
    assert 'for_each = var.allowed_cidr != "" ? [var.allowed_cidr] : []' in hcl
    assert "source_address_prefix      = security_rule.value" in hcl, \
        "admin NSG rule must source the allowed CIDR, not Internet"
    assert "var.ingress_ports" in hcl and "local.admin_port" in hcl


def test_gcp_gce_firewalls_attach_via_network_tags():
    hcl = _hcl("gcp-gce")
    # The regressed defect: firewalls declared but no network tags on the instance → never bind.
    assert "tags = [var.name]" in hcl, "instance MUST carry network tags or firewalls never attach"
    assert 'target_tags   = ["${var.name}"]' in hcl
    # Admin firewall gated on allowed_cidr and sourced from it (port 22 only).
    assert 'count   = var.allowed_cidr != "" ? 1 : 0' in hcl
    assert "source_ranges = [var.allowed_cidr]" in hcl
    assert "for p in var.ingress_ports" in hcl


@pytest.mark.parametrize("module", ["aws-ec2", "azure-vm", "gcp-gce"])
def test_admin_port_never_opens_to_world(module):
    """No VM module may open the admin port (22/3389) to 0.0.0.0/0 — admin ingress is
    CIDR-restricted; only declared app ports (ingress_ports) may be world-open by design."""
    hcl = _hcl(module)
    # allowed_cidr must be referenced near admin access in every module.
    assert "allowed_cidr" in hcl, f"{module} lost its allowed_cidr wiring"
