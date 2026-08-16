"""Production credential broker (P5.3 — Redesign/08 ADR-17, closes F-20).

The audit's most serious enterprise gap: one global long-lived key set for every tenant.
ADR-17's target — broker-issued, per-org, short-lived credentials (AWS AssumeRole / Azure
SP + workload identity / GCP SA impersonation), vault-backed, **dual-path during migration**.

What this module gives P5 (the architecture + the boundary, buildable + testable here):

- `CredentialGrant` — a scoped credential set that is REDACTION-SAFE BY CONSTRUCTION: its
  repr/str never reveal secret material, so it cannot leak into logs, traces, SSE events,
  EvidenceCards, prompts, or error messages. Only `.provider_env()` hands the raw material to
  the tool/subprocess layer that actually needs it.
- `resolve(org, provider, env, operation)` — the broker entry. The default backend
  (`EnvBackedBroker`) issues the process's configured credentials (the existing global set) —
  so with the flag OFF, behavior is byte-identical (dual-path fallback). A real vault/STS
  backend (AssumeRole/SP/impersonation) plugs in behind the same interface; that live
  integration needs a vault + cloud federation and is the P5.3 sign-off item (ADR-17).
- an audit hook: every grant is recorded (org/provider/env/operation/scope + a NON-secret
  fingerprint), never the secret.

Boundary: credentials NEVER enter the model layer, the harness, the engine, run_events, or
the frontend. Only the tool layer (Terraform env) receives `provider_env()`, and only when a
mutation is already governed/approved.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import structlog

from ..settings import Settings

log = structlog.get_logger(__name__)

Provider = Literal["aws", "azure", "gcp"]


class CredentialError(Exception):
    """No credential could be brokered for the requested scope."""


def global_provider_env(settings: Settings) -> dict[str, str]:
    """The process's configured provider env vars — the ONE source of truth for the
    provider→env mapping, used by both the dual-path broker default and the Terraform
    runner so the two never drift. Returns only what is configured (empty when absent)."""
    s = settings
    env: dict[str, str] = {}
    if s.aws_access_key_id:
        env["AWS_ACCESS_KEY_ID"] = s.aws_access_key_id
        env["AWS_SECRET_ACCESS_KEY"] = s.aws_secret_access_key
        env["AWS_DEFAULT_REGION"] = s.aws_default_region
        if s.aws_session_token:
            env["AWS_SESSION_TOKEN"] = s.aws_session_token
    if s.azure_client_id:
        env["ARM_CLIENT_ID"] = s.azure_client_id
        env["ARM_CLIENT_SECRET"] = s.azure_client_secret
        env["ARM_TENANT_ID"] = s.azure_tenant_id
        env["ARM_SUBSCRIPTION_ID"] = s.azure_subscription_id
    if s.google_cloud_project:
        env["GOOGLE_PROJECT"] = s.google_cloud_project
    if s.google_application_credentials:
        env["GOOGLE_APPLICATION_CREDENTIALS"] = s.google_application_credentials
    return env


@dataclass
class CredentialGrant:
    """A scoped, short-lived-by-intent credential set. Redaction-safe: the raw material lives
    in a private field whose contents never appear in repr/str/logs."""
    org_id: str | None
    provider: Provider
    env: str
    source: Literal["broker", "global_fallback"]
    scope: str                                   # e.g. "org:…/provider:aws/env:prod"
    expires_at: datetime | None = None
    _material: dict[str, str] = field(default_factory=dict, repr=False)

    def provider_env(self) -> dict[str, str]:
        """The raw env vars for the tool/subprocess layer ONLY. Callers must never log,
        emit, or return this — it is the one authorized egress of secret material."""
        return dict(self._material)

    def fingerprint(self) -> str:
        """A NON-secret stable id of this grant for audit (hash of the material keys +
        a salted digest of the principal id — never the secret values)."""
        principal = self._material.get("AWS_ACCESS_KEY_ID") or \
            self._material.get("ARM_CLIENT_ID") or \
            self._material.get("GOOGLE_PROJECT") or ""
        keys = ",".join(sorted(self._material))
        return hashlib.sha256(f"{self.scope}|{keys}|{principal[:6]}".encode()).hexdigest()[:16]

    # Redaction-safe representations — no secret ever renders.
    def __repr__(self) -> str:
        return (f"CredentialGrant(provider={self.provider!r}, env={self.env!r}, "
                f"source={self.source!r}, scope={self.scope!r}, keys={sorted(self._material)})")

    __str__ = __repr__


class CredentialBroker(ABC):
    @abstractmethod
    async def resolve(self, *, org_id: str | None, provider: Provider, env: str,
                      operation: str) -> CredentialGrant: ...


class EnvBackedBroker(CredentialBroker):
    """The dual-path default (ADR-17): issues the process's configured credentials as a
    `global_fallback` grant. Byte-identical to the pre-broker direct path — this is what runs
    until a per-org vault backend is signed off and configured (P5.3). It still delivers the
    boundary win: callers now receive a redaction-safe GRANT, not raw settings fields."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def resolve(self, *, org_id: str | None, provider: Provider, env: str,
                      operation: str) -> CredentialGrant:
        s = self.settings
        material: dict[str, str] = {}
        if provider == "aws" and s.aws_access_key_id:
            material = {"AWS_ACCESS_KEY_ID": s.aws_access_key_id,
                        "AWS_SECRET_ACCESS_KEY": s.aws_secret_access_key,
                        "AWS_DEFAULT_REGION": s.aws_default_region}
            if s.aws_session_token:
                material["AWS_SESSION_TOKEN"] = s.aws_session_token
        elif provider == "azure" and s.azure_client_id:
            material = {"ARM_CLIENT_ID": s.azure_client_id,
                        "ARM_CLIENT_SECRET": s.azure_client_secret,
                        "ARM_TENANT_ID": s.azure_tenant_id,
                        "ARM_SUBSCRIPTION_ID": s.azure_subscription_id}
        elif provider == "gcp" and s.google_cloud_project:
            material = {"GOOGLE_PROJECT": s.google_cloud_project}
            if s.google_application_credentials:
                material["GOOGLE_APPLICATION_CREDENTIALS"] = s.google_application_credentials
        if not material:
            raise CredentialError(
                f"no credentials configured for provider {provider!r} — a governed "
                f"mutation cannot proceed without a brokered credential")
        scope = f"org:{org_id or 'global'}/provider:{provider}/env:{env}"
        return CredentialGrant(org_id=org_id, provider=provider, env=env,
                               source="global_fallback", scope=scope, _material=material)


# A REGISTERED broker (a vault/STS backend from the P5.3 sign-off) overrides the default.
# When none is registered, the default EnvBackedBroker is built FRESH per call from the
# passed settings — never cached — so resolution always reflects the current settings
# (a cached default would ignore per-call settings, e.g. a different org's config).
_registered: CredentialBroker | None = None


def get_broker(settings: Settings) -> CredentialBroker:
    return _registered if _registered is not None else EnvBackedBroker(settings)


def set_broker(broker: CredentialBroker | None) -> None:
    global _registered
    _registered = broker


async def resolve(settings: Settings, *, org_id: str | None, provider: Provider,
                  env: str, operation: str) -> CredentialGrant:
    """Broker a credential grant for a governed operation, and AUDIT it (non-secret)."""
    grant = await get_broker(settings).resolve(
        org_id=org_id, provider=provider, env=env, operation=operation)
    try:
        from ..db import repositories as repo
        from ..db.session import session_scope
        import uuid as _uuid
        async with session_scope() as s:
            await repo.AuditRepo.log(
                s, org_id=_uuid.UUID(org_id) if org_id else None, actor="credential_broker",
                action="credential.grant", target=f"{provider}:{env}:{operation}",
                detail={"source": grant.source, "scope": grant.scope,
                        "fingerprint": grant.fingerprint()})   # NON-secret only
    except Exception as exc:  # noqa: BLE001 — audit best-effort; never blocks a governed op
        log.warning("credential_broker.audit_failed", error=str(exc))
    log.info("credential_broker.granted", provider=provider, env=env,
             source=grant.source, fingerprint=grant.fingerprint())   # no secret
    return grant
