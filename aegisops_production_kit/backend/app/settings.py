"""Typed application settings, loaded from environment / .env.

All runtime configuration comes from the environment (the operator fills `.env`).
Secrets are never hard-coded. Settings validate on startup; `/readyz` reports each
dependency's live health.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # The backend may run from the repo root or from backend/. Look in both places.
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App ──
    app_env: Literal["local", "dev", "staging", "production"] = "local"
    # S0 multi-tenancy. strict: every request is scoped to the authenticated principal's
    # organization (Keycloak `org` claim wins; the `users` mirror row by keycloak_sub /
    # username is the fallback for seeded users). legacy: pre-S0 single-default-org
    # behavior — rollback path only.
    aegisops_tenancy: Literal["strict", "legacy"] = "strict"
    # A5 4-eyes: when on, the initiator of a Production-environment run cannot approve it.
    aegisops_four_eyes_for_production: bool = True
    # S1 credential reveal: the step-up re-auth proof (a fresh Keycloak authentication) must
    # be no older than this many seconds. Password re-entry produces a proof dated "now".
    reveal_stepup_max_age_seconds: int = 120
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    cors_origins: str = "http://localhost:3000"
    secret_key: str = Field(default="change-me-to-a-long-random-string")
    rate_limit_per_minute: int = 120
    log_level: str = "INFO"

    # ── LLM: Google Gemini ──
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"
    gemini_embed_dim: int = 768  # output dimensionality requested; must match models.EMBED_DIM
    stream_word_delay_ms: int = 24
    # USD per 1M tokens, used to report cost on Langfuse generations (self-hosted Langfuse
    # has no built-in price table for Gemini models).
    gemini_cost_per_1m_input: float = 0.30
    gemini_cost_per_1m_output: float = 2.50

    # ── PostgreSQL + pgvector ──
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "aegisops"
    postgres_user: str = "aegisops"
    postgres_password: str = "aegisops"
    database_url: str = "postgresql+psycopg://aegisops:aegisops@localhost:5432/aegisops"

    # ── Redis ──
    redis_url: str = "redis://localhost:6379/0"

    # ── Neo4j ──
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "aegisops-neo4j"

    # ── Keycloak ──
    keycloak_url: str = "http://localhost:8080"
    # Browser-facing Keycloak origin for the SSO redirect. Inside docker the API reaches
    # Keycloak as http://keycloak:8080, but the USER'S BROWSER cannot resolve that service
    # name — the authorization redirect must use a host the browser can reach. Empty ⇒ same
    # as keycloak_url (correct for host-run dev where both are localhost:8080).
    keycloak_public_url: str = ""
    keycloak_realm: str = "aegisops"
    keycloak_client_id: str = "aegisops-backend"
    keycloak_client_secret: str = ""
    keycloak_admin: str = "admin"
    keycloak_admin_password: str = "admin"
    keycloak_frontend_client_id: str = "aegisops-frontend"

    # ── Langfuse ──
    langfuse_host: str = "http://localhost:3001"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # O2: the project the keys MUST belong to. On startup we verify the configured keys resolve
    # to this project and log a loud warning otherwise — so the "0 traces / wrong project"
    # regression (keys belonging to a different project in the same instance) can't recur silently.
    langfuse_expected_project: str = "aegisops"

    # ── OpenTelemetry / Prometheus / Grafana ──
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "aegisops-api"
    prometheus_url: str = "http://localhost:9090"
    grafana_url: str = "http://localhost:3002"

    # ── AWS ──
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""  # required for temporary/sandbox (STS) credentials
    aws_default_region: str = "us-east-1"
    tf_state_bucket: str = ""
    tf_state_dynamodb_table: str = ""
    tf_state_region: str = "us-east-1"

    # ── Azure ──
    azure_subscription_id: str = ""
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""

    # ── GCP ──
    google_cloud_project: str = ""
    google_application_credentials: str = ""

    # ── VMware vCenter ──
    vcenter_host: str = ""
    vcenter_user: str = ""
    vcenter_password: str = ""
    vcenter_insecure: bool = False

    # ── Kubernetes ──
    kubeconfig: str = ""

    # ── GitHub ──
    github_token: str = ""
    github_org: str = ""

    # ── ServiceNow ──
    servicenow_instance: str = ""
    servicenow_user: str = ""
    servicenow_password: str = ""

    # ── Email / notifications ──
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_from: str = "aegisops@yourcompany.com"

    # ── Terraform / Ansible ──
    terraform_bin: str = "terraform"
    ansible_bin: str = "ansible-playbook"
    terraform_workspaces_dir: str = "./infra/terraform-workspaces"
    default_execution_mode: Literal["dry_run", "plan", "apply", "destroy"] = "plan"

    @field_validator("cors_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def async_database_url(self) -> str:
        """SQLAlchemy async URL (psycopg3 async driver)."""
        url = self.database_url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+psycopg://", 1)
        return url

    @property
    def keycloak_realm_url(self) -> str:
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def keycloak_browser_url(self) -> str:
        """Keycloak origin the user's browser is sent to (falls back to keycloak_url)."""
        return (self.keycloak_public_url or self.keycloak_url).rstrip("/")

    @property
    def keycloak_jwks_url(self) -> str:
        return f"{self.keycloak_realm_url}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
