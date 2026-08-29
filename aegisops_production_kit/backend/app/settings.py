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
    # B1 event bus. memory: in-process asyncio channels (single-worker; the rollback default).
    # redis: Redis Streams (`run:<id>:events`) — worker-agnostic streaming + reconnect-anywhere,
    # required for horizontal scale. The Emitter/SSE frame contract is identical in both modes.
    aegisops_event_bus: Literal["memory", "redis"] = "memory"
    # B3: the periodic stranded-run reconciler background loop. `on` in real deployments; `off`
    # under pytest (set in the api-test service) so no background loop auto-starts in a
    # TestClient lifespan — tests that exercise the reconciler drive its `sweep()` explicitly.
    aegisops_reconciler: Literal["on", "off"] = "on"
    # D3: the drift/orphan reconciliation sweep (world model). Runs inside the reconciler loop;
    # `off` by default so tests (and installs without cloud read creds) drive `drift.sweep()`
    # explicitly. Turn on where live cloud reads are configured.
    aegisops_drift: Literal["on", "off"] = "off"
    # U6: the Governed Executive Loop — multi-step goal DAGs (create-first closures) execute
    # under one whole-DAG approval. `off` keeps the DEP dag branch as a text proposal.
    aegisops_exec_loop: Literal["on", "off"] = "off"
    # Intelligence layer (Prompt 2, 2026-08-17): the real retrieval pipeline (gate → planner
    # → multi-source → budgets → typed context) on the live build_context path. `off` reverts
    # to the pre-intelligence transcript+k3 behavior byte-for-byte.
    aegisops_intelligence: Literal["on", "off"] = "on"
    # Graphiti temporal knowledge layer (facts from revisions, episodes from consolidation,
    # temporal/semantic search). `off` (or an unreachable graph) degrades gracefully — the
    # deterministic PG/Neo4j answers still work; nothing fails.
    aegisops_graphiti: Literal["on", "off"] = "on"
    # P0 worker foundation: which background responsibilities this process owns.
    #   all    — API + background loops (single-node default; today's behavior)
    #   api    — serve HTTP/SSE only; no reconciler/retention/drift/gateway pollers
    #   worker — background loops (+ API surface remains available for health probes)
    # Under the api+api-b compose posture exactly ONE process runs the sweeps (F-18).
    aegisops_role: Literal["all", "api", "worker"] = "all"
    # P0/F-16: bearer token protecting GET /metrics. Empty + app_env=local → open (dev
    # ergonomics, keeps the compose Prometheus scrape working). Empty + non-local → 403.
    aegisops_metrics_token: str = ""
    # P0 ledger: local fsync'd spill journal for usage records that could not be
    # persisted to Postgres (replayed idempotently by the reconciler; gitignored).
    aegisops_ledger_spill_path: str = "./llm_usage_spill.jsonl"
    # P2.2 Agent Harness on READ paths (07 §2.2). Default OFF: when on, read-only triage
    # (SRE telemetry today) is driven by the kernel's OBSERVE→REASON→ACT loop over the
    # frozen investigation registry instead of the single hardcoded call. Mutation paths
    # are untouched (rule two). Old path remains the fallback — coexistence (T-P2-01).
    aegisops_harness_read_paths: str = "off"  # off | on
    # P3 durable execution / workflow engine (07 Phase 3). Default OFF: when on, a compiled
    # goal-DAG runs as a durable, wave-scheduled, restart-safe Workflow (app/engine) that
    # recovers from run_events + run_steps without repeating completed work. Mutation stays
    # governed (real Terraform apply remains the exec_loop/approval path); the existing
    # exec_loop remains the default path (coexistence, T-P3-01).
    aegisops_durable_engine: str = "off"  # off | on
    # P3.9: goal-DAG step ceiling raised 5→8 behind config (per-run concurrency capped by waves).
    aegisops_max_steps: int = 8
    # P4 capability packs (the harness-first inversion, 07 Phase 4). Default OFF — ships DARK
    # (07 risk #1): when on, the harness INV read registry is sourced from the AWS/Azure/GCP/
    # K8s/GitHub capability packs (cloud-neutral) instead of the hardcoded default registry.
    # The legacy path and cloudops.py remain; the production-spine cutover + agent dissolution
    # happen only at proven eval parity (T-P4-01). Mutation stays the governed exec_loop path.
    aegisops_capability_packs: str = "off"  # off | on
    # P4.5 default permission mode for a run (READ_ONLY | PLAN_ONLY | APPROVAL_REQUIRED).
    # AUTONOMOUS is intentionally NOT an accepted value here (never enabled in P4).
    aegisops_permission_mode: str = "APPROVAL_REQUIRED"
    # P5.3 credential broker (ADR-17, closes F-20). Default OFF: mutation credentials come
    # from the process's configured set exactly as before (dual-path fallback). When on, the
    # broker resolves a redaction-safe per-org grant; a vault/STS backend plugs in behind it
    # (the ADR-17 sign-off item). Either way credentials never reach prompts/logs/events/UI.
    aegisops_credential_broker: str = "off"  # off | on
    # Approval model (Redesign/00 §7): single-user HUMAN-IN-THE-LOOP. The initiating human
    # reviews and approves or rejects their own plan (initiator == approver). There is no
    # second-approver / four-eyes concept; the active posture is stamped on every approval
    # card + /healthz (P0.5) so it can never drift silently.
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
    # ── LLM: additional providers (P1.5; app/llm/config/models.yaml maps each provider
    # to its settings_field + wire family — no provider branch is hardcoded in code).
    # Empty key = provider unconfigured: it lists in the catalog but never routes.
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    # The openai_compat wire family serves ANY OpenAI-compatible endpoint;
    # empty base_url = the SDK default (api.openai.com).
    openai_base_url: str = ""
    # OpenRouter: its own provider identity + credentials, served by the openai_compat
    # wire family with a fixed base_url (declared in models.yaml).
    openrouter_api_key: str = ""
    # P1.6 budget gate (closes F-19's discoverability gap for LLM spend): 0 = off.
    # When > 0, the resilient executor refuses NEW model calls for an org whose
    # llm_usage cost for the current UTC day already exceeds this many USD.
    aegisops_llm_daily_budget_usd: float = 0.0

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
    # STAB P2-1 (KEYCLOAK_PUBLIC_URL pattern): browser-facing Langfuse origin for deep-links.
    # In compose the api reaches Langfuse at http://langfuse:3000 (in-network), which a
    # browser can't resolve (live: DNS_PROBE_FINISHED_NXDOMAIN, screenshot 3). "" = fall
    # back to langfuse_host (correct outside compose).
    langfuse_public_url: str = ""
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
    tf_state_key_prefix: str = "aegisops"
    # A3: TF backend. local (dev default) keeps per-module local state; remote supplies an
    # S3+DynamoDB backend via `-backend-config` at init (requires the module backend block to be
    # `s3` — a documented migration). Non-dev deployments set this to `remote`.
    aegisops_tf_backend: Literal["local", "remote"] = "local"

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

    # ── GW-1: messaging gateways (channel-agnostic seam; Telegram is the first adapter) ──
    # OFF by default: no poller starts, no route answers, until an operator turns it on.
    aegisops_telegram: Literal["on", "off"] = "off"
    telegram_bot_token: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    # Long-poll wait (seconds) handed to getUpdates. No public URL, no webhook — the API
    # calls Telegram, exactly like waku's gateway.
    telegram_poll_timeout_s: int = 25
    # One-time link code lifetime. Short: it is a bearer secret typed into a third-party chat.
    gateway_link_code_ttl_seconds: int = 600
    # Progressive-streaming throttle for edit-based streaming (Telegram rate-limits edits).
    gateway_edit_min_interval_ms: int = 1000
    gateway_edit_min_chars: int = 50
    # Browser-facing web origin used for deep links sent to a chat channel ("open in AegisOps").
    # "" ⇒ the first CORS origin, which is the browser origin in every posture we ship.
    web_public_url: str = ""

    # ── Email / notifications ──
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    notify_from: str = "aegisops@yourcompany.com"

    # ── Terraform / Ansible ──
    terraform_bin: str = "terraform"
    # LAT: skip `terraform init` when the module is already initialized (.terraform/ + lockfile
    # present) — the dominant per-turn cost (~19s cold). Escape hatch: set false to force full
    # init on every run. A shared provider plugin cache (TF_PLUGIN_CACHE_DIR, a named volume in
    # compose) makes even the cold init cheap by reusing downloaded providers across modules.
    aegisops_tf_skip_init_when_ready: bool = True
    tf_plugin_cache_dir: str = ""
    # STAB P0-1: when set, each module's .terraform lives under <tf_data_root>/<module>
    # (TF_DATA_DIR) on a NATIVE volume instead of the 9p/OneDrive-backed workspaces bind
    # mount. Providers then symlink from the shared cache (same filesystem) and the
    # provider binary executes off ext4 — measured aws-s3 plan: 84-94s on 9p → 5s native.
    # "" = off (module-dir .terraform, the pre-P0-1 behavior). State files are untouched
    # (terraform.tfstate.d stays in the module dir).
    tf_data_root: str = ""
    ansible_bin: str = "ansible-playbook"
    terraform_workspaces_dir: str = "./infra/terraform-workspaces"
    default_execution_mode: Literal["dry_run", "plan", "apply", "destroy"] = "plan"
    # PR-2b: per-stage subprocess budgets. Expiry = process-GROUP kill (TERM→grace→KILL),
    # run fails honestly, the reconciler/orphan sweep reconciles any leftover state lock.
    tf_plan_timeout_s: int = 600       # init/plan/show
    tf_apply_timeout_s: int = 2700     # apply/destroy
    # PR-2a: max concurrent ACTIVE runs (heartbeat-derived — never a drifting counter).
    max_active_runs_per_org: int = 5
    max_active_runs_per_user: int = 2
    # PR-4: retention sweeper — ALL OFF (0) by default in dev. Prod defaults documented in
    # .env.example. audit_log + approvals are NEVER auto-deleted (compliance).
    retention_messages_days: int = 0        # delete msgs/run_steps for CLOSED sessions beyond N days
    retention_notifications_days: int = 0   # delete notifications beyond N days
    retention_run_plan_days: int = 0        # compact bulky run plan_json beyond N days (keep the row)

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

    @property
    def web_base_url(self) -> str:
        """The origin a deep link sent to a chat channel must use. Same reasoning as
        `keycloak_browser_url` / `langfuse_public_url`: an in-network service name is
        unreachable from the user's phone."""
        if self.web_public_url:
            return self.web_public_url.rstrip("/")
        origins = self.cors_origin_list
        return origins[0].rstrip("/") if origins else "http://localhost:3000"


@lru_cache
def get_settings() -> Settings:
    return Settings()
