"""SQLAlchemy ORM models — all AegisOps entities.

Multi-tenant: every tenant-scoped row carries `org_id`; repositories scope every query
to the caller's org. Embeddings live in `document_chunks.embedding` (pgvector). Approval
and audit rows are treated as immutable (insert-only; never updated/deleted in app code).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
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
    # governance facts: 4-eyes compares approver vs initiator for Production changes, and S1
    # gates the credential reveal on initiator-or-approver.
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    env: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Production | Staging | …
    intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    routing_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    domain: Mapped[str | None] = mapped_column(String(40), nullable=True)  # cloudops|devops|sre|knowledge|general
    workflow: Mapped[str | None] = mapped_column(String(80), nullable=True)
    workflow_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    mode: Mapped[str] = mapped_column(String(20), default="plan")  # dry_run|plan|apply|destroy
    status: Mapped[str] = mapped_column(String(30), default="running")
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
    status: Mapped[str] = mapped_column(String(30), default="pending")  # pending|running|done|failed|cancelled
    tool: Mapped[str | None] = mapped_column(String(60), nullable=True)
    human_vs_auto: Mapped[str] = mapped_column(String(10), default="auto")  # auto | human
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, default=0)

    run: Mapped[Run] = relationship(back_populates="steps")


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
