"""Production configuration preflight (P5 hardening — Redesign/07 Phase 5, 09 readiness).

A single place that validates the process is safely configured for its environment, run at
startup (after the existing P0 event-bus/Redis refusals) and exposed as a structured report
for `/readyz`. It ADDS checks; it does not replace the P0 startup refusals (which stay the
hard gate). Findings are classified `ok | warn | block`; a `block` in a non-local env means
the process must not serve.

No secret is ever included in a finding — only whether credentials are present, never their
values (the credential-broker boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .settings import Settings

Severity = Literal["ok", "warn", "block"]


@dataclass
class Finding:
    check: str
    severity: Severity
    detail: str


@dataclass
class PreflightReport:
    app_env: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def as_dict(self) -> dict:
        return {"app_env": self.app_env, "blocked": self.blocked,
                "findings": [{"check": f.check, "severity": f.severity, "detail": f.detail}
                             for f in self.findings]}


def run(settings: Settings) -> PreflightReport:
    """Validate production-safe posture. Blocks are enforced only in non-local envs — local
    dev keeps booting with warnings (parity with the P0 policy)."""
    s = settings
    local = s.app_env == "local"
    r = PreflightReport(app_env=s.app_env)

    def add(check: str, ok: bool, block_detail: str, *, warn_only: bool = False) -> None:
        if ok:
            r.findings.append(Finding(check, "ok", "ok"))
        else:
            sev: Severity = "warn" if (local or warn_only) else "block"
            r.findings.append(Finding(check, sev, block_detail))

    # Production/multi-replica coordination MUST be Redis (no silent memory fallback).
    add("event_bus", not (not local and s.aegisops_event_bus == "memory"),
        "non-local requires AEGISOPS_EVENT_BUS=redis")
    # /metrics must be authenticated off-local (F-16).
    add("metrics_auth", bool(s.aegisops_metrics_token) or local,
        "AEGISOPS_METRICS_TOKEN must be set off-local (/metrics auth)")
    # AUTONOMOUS must never be the configured permission mode (governed product).
    add("permission_mode", getattr(s, "aegisops_permission_mode", "APPROVAL_REQUIRED")
        != "AUTONOMOUS", "AUTONOMOUS permission mode is not permitted")
    # Single-user HITL: four-eyes must not have been reintroduced as a setting.
    add("no_four_eyes", not hasattr(s, "aegisops_four_eyes_for_production"),
        "four-eyes setting must not exist (single-user HITL)")
    # Strict tenancy off-local.
    add("tenancy", s.aegisops_tenancy == "strict" or local,
        "AEGISOPS_TENANCY=strict required off-local")
    # A worker owner must exist so background sweeps run exactly once (F-18).
    add("worker_role", getattr(s, "aegisops_role", "all") in ("all", "worker") or local,
        "no worker role owns background sweeps (set AEGISOPS_ROLE=all|worker somewhere)",
        warn_only=True)
    # Prod-hardening (2026-08-17): shipped defaults must never survive into a real env.
    add("secret_key", local or (s.secret_key != "change-me-to-a-long-random-string"
                                and len(s.secret_key) >= 32),
        "SECRET_KEY is the shipped default or shorter than 32 chars — set a long random value")
    add("keycloak_admin_password", local or s.keycloak_admin_password != "admin",
        "KEYCLOAK_ADMIN_PASSWORD is the shipped default 'admin'")
    # CORS wildcard off-local turns every browser into a caller of an authenticated API.
    add("cors_origins", local or "*" not in s.cors_origin_list,
        "CORS_ORIGINS contains '*' — off-local requires an explicit origin allowlist")
    # Production credentials: off-local, prefer the broker over the global set (F-20/ADR-17).
    if not local and getattr(s, "aegisops_credential_broker", "off") != "on" and (
            s.aws_access_key_id or s.azure_client_id or s.google_cloud_project):
        r.findings.append(Finding(
            "credential_broker", "warn",
            "off-local with a global credential set and the broker off — enable "
            "AEGISOPS_CREDENTIAL_BROKER for per-org scoping (ADR-17)"))
    else:
        r.findings.append(Finding("credential_broker", "ok", "ok"))
    return r
