"""SQLAlchemy ORM models — all AegisOps entities.

Multi-tenant: every tenant-scoped row carries `org_id`; repositories scope every query
to the caller's org. Embeddings live in `document_chunks.embedding` (pgvector). Approval
and audit rows are treated as immutable (insert-only; never updated/deleted in app code).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base

# Must match settings.gemini_embed_dim (the requested embedding output dimensionality).
EMBED_DIM = 768


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    plan: Mapped[str] = mapped_column(String(40), default="enterprise")
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)  # kebab realm role
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("keycloak_sub", name="uq_users_keycloak_sub"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    keycloak_sub: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str] = mapped_column(String(160), nullable=False)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Session(Base):
    """A chat session (conversation thread). Terminal when its SR/CR/INC is closed."""

    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | closed
    # GW-1: which gateway opened this conversation (web | telegram | …). Sessions are
    # per-channel, so channel provenance is a session-level fact.
    source: Mapped[str] = mapped_column(String(20), default="web")
    snow_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    messages: Mapped[list[Message]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, default="")
    confidentiality_level: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Low|Medium|High
    confidentiality_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    context_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snow_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # reasoning summary + references
    # M2: per-message embedding for semantic conversational recall (pgvector). Nullable — a
    # no-Gemini setup leaves it NULL and recall degrades to pg_trgm keyword search.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[Session] = relationship(back_populates="messages")


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    value: Mapped[str] = mapped_column(String(8), nullable=False)  # up | down
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    # A5: who started the run (users.id mirror row) + the environment it targets — both are
    # governance facts: S1 gates the credential reveal on initiator-or-approver, and PR-3
    # gates cancel the same way; the audit trail records who initiated every change.
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    env: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Production | Staging | …
    # GW-1: which gateway initiated this run (web | telegram | …) — a governance fact, so an
    # audit can answer "was this Production change started from a phone?".
    source: Mapped[str] = mapped_column(String(20), default="web")
    intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(40), nullable=True)  # cloudops|devops|sre|knowledge|general
    workflow: Mapped[str | None] = mapped_column(String(80), nullable=True)
    workflow_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="plan")  # dry_run|plan|apply|destroy
    status: Mapped[str] = mapped_column(String(30), default="running")
    # P3: optional link to the Task container spanning runs (06 §8.1; migration 0015).
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    plan_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    input_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    context_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    snow_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list[RunStep]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunStep(Base):
    __tablename__ = "run_steps"
    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending|running|done|failed|cancelled|compensated
    tool: Mapped[str | None] = mapped_column(String(60), nullable=True)
    human_vs_auto: Mapped[str] = mapped_column(String(10), default="auto")  # auto | human
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    # P3 durable-engine columns (06 §8.1 Step; migration 0015). Nullable — the legacy
    # exec_loop path ignores them, so old rows stay valid (coexistence).
    wave: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depends_on: Mapped[str | None] = mapped_column(String(120), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str | None] = mapped_column(String(16), nullable=True)  # module|day2|k8s|read|gate
    compensation_of: Mapped[str | None] = mapped_column(String(120), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    run: Mapped[Run] = relationship(back_populates="steps")


class Task(Base):
    """P3 user-visible objective container that may span runs (Redesign/06 §8.1; migration
    0015). A Run gains an optional `task_id` back-reference; legacy runs have none."""

    __tablename__ = "tasks"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="open")  # open|running|completed|failed|cancelled
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))


class Approval(Base):
    """Immutable audit of a human approval decision."""

    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)  # approved | rejected
    actor_user: Mapped[str] = mapped_column(String(160), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(60), nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source: Mapped[str | None] = mapped_column(String(120), nullable=True)
    kind: Mapped[str] = mapped_column(String(40), default="runbook")  # runbook|rca|design-doc|summary
    uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, default="")
    doc_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    chunks: Mapped[list[DocumentChunk]] = relationship(back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Nullable so documents can be seeded before an embedding model/key is configured.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    document: Mapped[Document] = relationship(back_populates="chunks")


class AuditLog(Base):
    """Immutable audit log (insert-only)."""

    __tablename__ = "audit_log"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(160), default="system")
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    target: Mapped[str | None] = mapped_column(String(200), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    correlation: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # trace/context/session ids
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    config_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="unknown")
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Resource(Base):
    """Provisioned-resource inventory (day-2). One row per resource the platform created, so it
    can be referenced by name/context later ("test-vm", "the instance I created") and operated on.
    Org-scoped. `inputs` stores the validated Terraform variables used, so a modify re-plan can be
    reconstructed; `attributes` holds live-ish key attributes (IPs, VPC, subnet, SGs, tags)."""

    __tablename__ = "resources"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)  # stable name (e.g. test-vm)
    cloud: Mapped[str] = mapped_column(String(20), nullable=False)
    region: Mapped[str | None] = mapped_column(String(40), nullable=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False)  # aws_instance | vpc | s3_bucket | ...
    provider_id: Mapped[str | None] = mapped_column(String(200), nullable=True)  # i-…, vpc-…
    workspace: Mapped[str | None] = mapped_column(String(120), nullable=True)  # TF module dir (aws-ec2, …)
    # Per-resource Terraform state workspace slug (Phase 8 / N-08 isolation). NULL = legacy
    # resource in the module's default workspace.
    state_workspace: Mapped[str | None] = mapped_column(String(80), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | terminated | destroyed
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # ips, vpc_id, subnet_id, sgs, tags, key_name…
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # validated TF vars (to rebuild a modify plan)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class UserMemory(Base):
    """M4: user-editable standing context that survives sessions ("my usual region is
    ap-south-1"). Org-scoped; user_id NULL = org-wide. One row per (org, user, key)."""

    __tablename__ = "user_memories"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ChannelIdentity(Base):
    """GW-1: a messaging-channel account BOUND to a platform user (org-scoped).

    Identity is the binding, never a chat-id allowlist. An unbound sender has no identity on
    this platform and receives only the how-to-link reply — it cannot start a run, read
    anything, or approve anything. Once bound, RBAC and tenancy follow the bound
    user everywhere, exactly as they do on the web.

    `active_session_id` gives the channel "one chat = one session per user"; `/new` clears it.
    """

    __tablename__ = "channel_identities"
    __table_args__ = (
        # One channel account may map to at most one platform user…
        UniqueConstraint("channel", "channel_user_id", name="uq_channel_identity_account"),
        # …and one platform user holds at most one account per channel.
        UniqueConstraint("channel", "user_id", name="uq_channel_identity_user"),
    )
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)          # "telegram"
    channel_user_id: Mapped[str] = mapped_column(String(64), nullable=False)  # numeric id, as text
    channel_chat_id: Mapped[str] = mapped_column(String(64), nullable=False)  # where replies go
    channel_username: Mapped[str | None] = mapped_column(String(160), nullable=True)
    active_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    linked_by: Mapped[str | None] = mapped_column(String(160), nullable=True)  # web user who linked
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChannelLinkCode(Base):
    """GW-1: a one-time, short-lived code that binds a channel account to its issuer.

    The code is a bearer secret the user types into a third-party chat app, so only its
    SHA-256 hash is stored — reading this table cannot harvest a live code. Single-use
    (`used_at`) and expiring (`expires_at`); both are enforced in `gateways.identity`.
    """

    __tablename__ = "channel_link_codes"
    __table_args__ = (UniqueConstraint("code_hash", name="uq_channel_link_code_hash"),)
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    used_by_channel_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModuleProposal(Base):
    """MPP: a drafted Terraform module moving through draft → checks → proposed →
    promoted|rejected. Only PROMOTED rows join the approved library; a draft is inert data —
    generation and execution never happen in the same turn."""

    __tablename__ = "module_proposals"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80), nullable=False)   # e.g. "aws.efs"
    cloud: Mapped[str] = mapped_column(String(20), nullable=False)
    resource: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    files: Mapped[dict] = mapped_column(JSONB, nullable=False)     # {filename: content}
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft|proposed|promoted|rejected
    fmt_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validate_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    scan: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {tool,status,findings}
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[uuid.UUID] = _pk()
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    level: Mapped[str] = mapped_column(String(20), default="info")
    color: Mapped[str | None] = mapped_column(String(40), nullable=True)
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LlmUsage(Base):
    """P0 authoritative LLM usage/cost ledger (Redesign/06 §8.2; migration 0010).

    Accounting truth — Langfuse is observability. Append-only by convention. `id` is
    client-generated so retry/spill-replay inserts are idempotent (ON CONFLICT DO
    NOTHING). `org_id`/`run_id` carry no FKs on purpose: an accounting record must
    outlive the rows it describes, and spill replay must never fail an FK check.
    Tokens are ground truth; `cost_usd` is a write-time convenience snapshot.
    """

    __tablename__ = "llm_usage"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # arrives with P2/P3
    purpose: Mapped[str] = mapped_column(String(40), default="legacy")
    provider: Mapped[str] = mapped_column(String(20), default="google")
    model: Mapped[str] = mapped_column(String(80))
    requested_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    agent_kind: Mapped[str] = mapped_column(String(20), default="main")
    prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)  # P2 registry
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(60), default="ok")


class ModelBinding(Base):
    """P1.7 org-level model routing override: who runs what (Redesign/04 §4.4, 06 §8.2).

    `models.yaml` says what CAN run; this row says which model an org's purpose actually
    uses. Eval-gated promotion: `eval_state` is the release control — a `failed` binding
    never routes (router filter); every write is validated against the catalog's
    capability requirements and lands an audit row.
    """

    __tablename__ = "model_bindings"
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), primary_key=True)
    model: Mapped[str] = mapped_column(String(120))
    # pending | passed | failed | waived (04 §4.4)
    eval_state: Mapped[str] = mapped_column(String(12), default="pending")
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))


class RunEvent(Base):
    """P2.5 durable, event-sourced run log (Redesign/06 §8.2, ADR-16; migration 0012).

    Append-only by convention; `seq` per-run monotonic and GAPLESS (10 §0 invariant 1);
    payloads redacted BEFORE the write. `run_id`/`org_id` carry no FKs on purpose: the
    record must outlive the rows it describes. The 18th kind, `agent_gate`, is the C-05
    resolution (retrieval-gate decisions are observable events, per doc 10 scenario Q).
    """

    __tablename__ = "run_events"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                         default=lambda: datetime.now(UTC))
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),)


class MemoryItem(Base):
    """P2.6 episodic/semantic memory tier (Redesign/06 §1; migration 0013).

    Written ONLY by consolidation proposals (human-accepted) or humans — never a direct
    agent write (06 §2). Contradiction ⇒ supersede, not coexist. 768-d pgvector matches
    the pinned embedding dimension (ADR-02)."""

    __tablename__ = "memory_items"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)         # fact | episode
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    provenance: Mapped[str] = mapped_column(String(24), nullable=False)
    origin_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    status: Mapped[str] = mapped_column(String(16), default="active")
    supersedes: Mapped[int | None] = mapped_column(
        ForeignKey("memory_items.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))
    created_by: Mapped[str | None] = mapped_column(String(120), nullable=True)


class PromptRegistry(Base):
    """P2.8 versioned prompt artifact (Redesign/05 §9; migration 0014). PromptRef(name,
    version) resolves here; content_hash + eval_state make prompt changes auditable and
    gate-able ('which prompt caused this regression?')."""

    __tablename__ = "prompt_registry"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    owner: Mapped[str | None] = mapped_column(String(120), nullable=True)
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    eval_state: Mapped[str] = mapped_column(String(12), default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC))
