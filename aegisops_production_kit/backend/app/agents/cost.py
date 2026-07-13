"""COST — static catalog cost estimation feeding the approval card + policy checks.

Owner directive (2026-07-13): a simple static provider-pricing estimate, NOT Infracost
(that integration is backlog). The numbers below are a maintained on-demand catalog
(us-east-1 / us-central1 / eastus, USD/month at 730h) and every card row says so —
"static catalog estimate", never presented as a quote.

Guardrail: `AEGISOPS_COST_GUARDRAIL_USD` > 0 turns on a REAL policy check that FAILS the
card when the estimate breaches the cap (the approver sees it; approval stays possible,
exactly like any failed policy check). Estimate unavailable for a priced-guardrail run →
the guardrail check fails closed ("cannot verify").
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_HOURS = 730

# ── the catalog (USD) ──────────────────────────────────────────────────────────────────────
_EC2_HOURLY = {
    "t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832,
    "t3.xlarge": 0.1664, "m5.large": 0.096, "m5.xlarge": 0.192, "m6i.large": 0.096,
    "m6i.xlarge": 0.192, "c5.large": 0.085, "r5.large": 0.126,
}
_RDS_HOURLY = {
    "db.t3.micro": 0.017, "db.t3.small": 0.034, "db.t3.medium": 0.068,
    "db.t3.large": 0.136, "db.m5.large": 0.171, "db.r5.large": 0.24,
}
_GCE_HOURLY = {
    "e2-micro": 0.008378, "e2-small": 0.016756, "e2-medium": 0.033511,
    "n2-standard-2": 0.097118, "n2-standard-4": 0.194236, "e2-standard-2": 0.067006,
}
_AZ_VM_HOURLY = {
    "Standard_B1s": 0.0104, "Standard_B2s": 0.0416, "Standard_B1ms": 0.0207,
    "Standard_D2s_v5": 0.096, "Standard_E2s_v5": 0.126,
}
_AZ_DB_HOURLY = {  # flexible-server compute tiers
    "B_Standard_B1ms": 0.017, "B_Standard_B2s": 0.034, "GP_Standard_D2s_v3": 0.171,
}
_CSQL_HOURLY = {"db-f1-micro": 0.0105, "db-g1-small": 0.035, "db-custom-2-7680": 0.0966}

_GB_MONTH = {"ebs_gp3": 0.08, "rds_gp2": 0.115, "pg_flex": 0.115, "csql_ssd": 0.17}

_FLAT_MONTHLY = {
    "nlb": 0.0225 * _HOURS,          # AWS NLB hourly (LCU excluded — usage-based, noted)
    "nat_aws": 0.045 * _HOURS,       # per NAT gateway, data excluded
    "nat_gcp": 0.045 * _HOURS,
    "nat_azure": 0.045 * _HOURS,
    "eks_cp": 0.10 * _HOURS,         # EKS control plane
    "gke_cp": 0.10 * _HOURS,         # GKE standard control plane
    "aks_cp": 0.0,                   # AKS free tier control plane
    "kms_key_aws": 1.0,              # per key per month
    "kms_key_gcp": 0.06,             # per active key version
    "log_analytics_base": 2.30,      # per GB ingested — base note only
}


@dataclass
class Estimate:
    monthly_usd: float | None        # None = not in the catalog (usage-based/unpriced)
    notes: list[str]

    @property
    def text(self) -> str:
        if self.monthly_usd is None:
            return "not in the static catalog (usage-based or unpriced)"
        return f"${self.monthly_usd:,.2f}/mo — static catalog estimate"


def guardrail_usd() -> float:
    try:
        return float(os.getenv("AEGISOPS_COST_GUARDRAIL_USD", "0") or 0)
    except ValueError:
        return 0.0


def estimate(template_key: str, inputs: dict) -> Estimate:
    """Static catalog estimate for one resource's month. Honest Nones for usage-based."""
    i = inputs or {}
    notes: list[str] = []

    if template_key == "aws.ec2":
        hourly = _EC2_HOURLY.get(str(i.get("instance_type", "")))
        if hourly is None:
            return Estimate(None, [f"instance type {i.get('instance_type')} not in the catalog"])
        vol = int(i.get("root_volume_size") or 0) or 30
        total = hourly * _HOURS + vol * _GB_MONTH["ebs_gp3"]
        if i.get("power_state") == "stopped":
            total = vol * _GB_MONTH["ebs_gp3"]
            notes.append("stopped: compute unbilled, EBS storage still accrues")
        return Estimate(round(total, 2), notes)

    if template_key == "aws.rds":
        hourly = _RDS_HOURLY.get(str(i.get("instance_class", "")))
        if hourly is None:
            return Estimate(None, [f"instance class {i.get('instance_class')} not in the catalog"])
        gb = int(i.get("allocated_storage") or 20)
        return Estimate(round(hourly * _HOURS + gb * _GB_MONTH["rds_gp2"], 2), notes)

    if template_key == "gcp.vm":
        hourly = _GCE_HOURLY.get(str(i.get("machine_type", "")))
        if hourly is None:
            return Estimate(None, [f"machine type {i.get('machine_type')} not in the catalog"])
        total = hourly * _HOURS
        if i.get("spot"):
            total *= 0.35
            notes.append("spot pricing (~65% off on-demand; variable)")
        if i.get("power_state") == "stopped":
            total = 0.0
            notes.append("stopped: compute unbilled, disk still accrues (unpriced here)")
        return Estimate(round(total, 2), notes)

    if template_key == "azure.vm":
        hourly = _AZ_VM_HOURLY.get(str(i.get("size", "")))
        if hourly is None:
            return Estimate(None, [f"size {i.get('size')} not in the catalog"])
        return Estimate(round(hourly * _HOURS, 2), notes)

    if template_key in ("azure.db", "azure.postgres"):
        hourly = _AZ_DB_HOURLY.get(str(i.get("sku_name", "")))
        if hourly is None:
            return Estimate(None, [f"sku {i.get('sku_name')} not in the catalog"])
        gb = max(32, int(i.get("storage_mb") or 32768) // 1024)
        return Estimate(round(hourly * _HOURS + gb * _GB_MONTH["pg_flex"], 2), notes)

    if template_key == "gcp.cloudsql":
        hourly = _CSQL_HOURLY.get(str(i.get("tier", "")))
        if hourly is None:
            return Estimate(None, [f"tier {i.get('tier')} not in the catalog"])
        return Estimate(round(hourly * _HOURS + 10 * _GB_MONTH["csql_ssd"], 2),
                        notes + ["assumes the 10GB default disk"])

    if template_key == "aws.nlb":
        return Estimate(round(_FLAT_MONTHLY["nlb"], 2), ["LCU usage excluded (traffic-based)"])

    if template_key == "aws.vpc":
        if i.get("enable_nat", True):
            n = int(i.get("az_count") or 3)
            return Estimate(round(_FLAT_MONTHLY["nat_aws"] * n, 2),
                            [f"{n} NAT gateway(s); data processing excluded"])
        return Estimate(0.0, ["VPC itself is free; NAT disabled"])

    if template_key in ("gcp.vpc", "azure.vnet"):
        nat = i.get("enable_nat", True) if template_key == "gcp.vpc" else i.get("nat_enabled", True)
        key = "nat_gcp" if template_key == "gcp.vpc" else "nat_azure"
        if nat:
            return Estimate(round(_FLAT_MONTHLY[key], 2), ["NAT gateway; data excluded"])
        return Estimate(0.0, ["network itself is free"])

    if template_key == "aws.eks":
        nodes = int(i.get("desired_size") or 3)
        node_hourly = _EC2_HOURLY.get(str((i.get("instance_types") or ["m6i.xlarge"])[0]), 0.192)
        return Estimate(round(_FLAT_MONTHLY["eks_cp"] + nodes * node_hourly * _HOURS, 2),
                        [f"control plane + {nodes} nodes (standard mode; Auto Mode is usage-based)"])

    if template_key == "gcp.gke":
        nodes = int(i.get("node_count") or 2)
        node_hourly = _GCE_HOURLY.get(str(i.get("machine_type", "e2-medium")), 0.033511)
        return Estimate(round(_FLAT_MONTHLY["gke_cp"] + nodes * node_hourly * _HOURS, 2),
                        [f"control plane + {nodes} nodes"])

    if template_key == "azure.aks":
        nodes = int(i.get("node_count") or 2)
        node_hourly = _AZ_VM_HOURLY.get(str(i.get("node_size", "Standard_B2s")), 0.0416)
        extra = []
        if i.get("enable_monitoring"):
            extra.append("Log Analytics ingestion billed per GB (excluded)")
        return Estimate(round(nodes * node_hourly * _HOURS, 2),
                        [f"free control plane + {nodes} nodes"] + extra)

    if template_key == "aws.kms":
        return Estimate(_FLAT_MONTHLY["kms_key_aws"], ["per key; API requests excluded"])

    if template_key == "gcp.kms":
        keys = max(1, len(i.get("keys") or []) or 1)
        return Estimate(round(_FLAT_MONTHLY["kms_key_gcp"] * keys, 2),
                        [f"{keys} key version(s); operations excluded"])

    if template_key in ("aws.s3", "gcp.gcs", "azure.storage", "azure.keyvault",
                        "azure.resource_group"):
        return Estimate(0.0, ["usage-based (storage/requests) — no fixed monthly charge"])

    return Estimate(None, [])


def checks_for(template_key: str, inputs: dict) -> list[dict]:
    """Policy-check rows for the approval card: the estimate (always stated) and, when a
    guardrail is configured, a REAL pass/fail check against it."""
    est = estimate(template_key, inputs)
    detail = est.text + ("" if not est.notes else " · " + "; ".join(est.notes))
    checks = [{"name": "Cost estimate (catalog)", "passed": True, "detail": detail}]
    cap = guardrail_usd()
    if cap > 0:
        if est.monthly_usd is None:
            checks.append({"name": f"Cost guardrail (≤ ${cap:,.0f}/mo)", "passed": False,
                           "detail": "cannot verify — the estimate is not in the static catalog"})
        else:
            checks.append({"name": f"Cost guardrail (≤ ${cap:,.0f}/mo)",
                           "passed": est.monthly_usd <= cap,
                           "detail": f"${est.monthly_usd:,.2f}/mo vs the ${cap:,.0f} cap"})
    return checks
