"""Seed real initial data into the datastores (idempotent).

Run via `make seed` after `make migrate`. Loads the organization, the 8 RBAC roles, the
seed users, the integrations registry, the Knowledge documents (chunked + embedded into
pgvector when a Gemini key is present), and seed notifications — derived from the design's
seed values. Safe to run repeatedly.
"""

from __future__ import annotations

import asyncio
import sys

import psycopg
from sqlalchemy import select

from app.db import session as db
from app.db.models import Document, Notification, Organization, Role, User
from app.db.repositories import AuditRepo, IntegrationRepo
from app.logging_conf import configure_logging, get_logger
from app.rag import ingest
from app.security import rbac
from app.settings import get_settings

log = get_logger("seed")

# S0 multi-tenancy: TWO organizations are seeded so isolation is demonstrable end-to-end
# (API + UI). Slugs match the Keycloak org groups in infra/keycloak/realm-export.json.
ORG_NAME = "Northwind Financial"
ORG_SLUG = "northwind-financial"

SEED_USERS = [
    {"username": "maya.okafor", "email": "maya.okafor@northwind.com", "name": "Maya Okafor", "roles": ["platform-admin"]},
    {"username": "dev.engineer", "email": "dev.engineer@northwind.com", "name": "Dev Engineer", "roles": ["devops-engineer"]},
    {"username": "audit.viewer", "email": "audit.viewer@northwind.com", "name": "Audit Viewer", "roles": ["read-only"]},
]

ORG_B_NAME = "Acme Industrial"
ORG_B_SLUG = "acme-industrial"

ORG_B_USERS = [
    {"username": "bob.chen", "email": "bob.chen@acme-industrial.com", "name": "Bob Chen", "roles": ["org-admin"]},
    {"username": "eve.ops", "email": "eve.ops@acme-industrial.com", "name": "Eve Ops", "roles": ["devops-engineer"]},
]

ORG_B_DOCUMENTS = [
    {
        "title": "Acme plant-floor VPN runbook",
        "source": "runbook", "kind": "runbook", "uri": "kb://acme/plant-floor-vpn",
        "content": (
            "Acme Industrial — plant-floor VPN runbook\n\n"
            "Site-to-site VPN between the acme-ot VPC (10.80.0.0/16) and the plant-floor network. "
            "Tunnels terminate on the acme-vgw gateway; BGP over two tunnels for failover. "
            "Any change to the tunnel configuration requires a change request and approval."
        ),
    },
]

ORG_B_NOTIFICATIONS = [
    ("Welcome to AegisOps · Acme Industrial", "info", "var(--green)"),
]

INTEGRATIONS = [
    ("Keycloak", "Identity · SSO/SAML", "connected"),
    ("LangGraph", "Agent orchestration", "active"),
    ("Langfuse", "LLM observability", "tracing"),
    ("OpenTelemetry", "Traces · metrics", "connected"),
    ("Prometheus", "Metrics", "scraping"),
    ("Grafana", "Dashboards", "connected"),
    ("PostgreSQL", "Primary datastore", "healthy"),
    ("Redis", "Cache · queues", "healthy"),
    ("Neo4j", "Context graph", "connected"),
    ("Terraform", "Provisioning", "connected"),
    ("Ansible", "Configuration", "connected"),
    ("GitHub", "SCM · Actions", "connected"),
    ("ServiceNow", "ITSM · SR/CR/INC", "syncing"),
]

NOTIFICATIONS = [
    ("Approval requested · EKS production plan", "warn", "var(--amber)"),
    ("INC-2291 checkout latency · assigned to you", "error", "var(--red)"),
    ("deploy orders-api v4.2.1 succeeded", "info", "var(--green)"),
    ("Drift detected · data-lakehouse staging", "info", "var(--cyan)"),
]

DOCUMENTS = [
    {
        "title": "EKS Production Hardening",
        "source": "runbook", "kind": "runbook", "uri": "kb://runbooks/eks-production-hardening",
        "content": (
            "EKS Production Hardening Runbook\n\n"
            "All production EKS clusters must enable a private API endpoint only; the public endpoint is disabled. "
            "Secrets are encrypted at rest with a dedicated KMS key (encryption_config resources = secrets). "
            "Node groups use IRSA (IAM Roles for Service Accounts) so no static IAM keys live on nodes.\n\n"
            "Networking reuses the existing production VPC; no new VPC is created. Worker nodes run only in private "
            "subnets across three availability zones with NAT egress. The org-approved module is terraform-aws-eks v20.8.\n\n"
            "Before apply, policy evaluation must pass six checks: secrets encryption, private endpoint, IRSA, approved "
            "module version, cost within the $500/mo guardrail, and tag compliance."
        ),
    },
    {
        "title": "Payments Platform Architecture v3",
        "source": "design-doc", "kind": "design-doc", "uri": "kb://design/payments-platform-v3",
        "content": (
            "Payments Platform Architecture v3\n\n"
            "payments-platform runs on EKS in us-east-1 across 3 AZs, fronted by an internal ALB. The prod VPC "
            "vpc-0a91c4f2 (10.40.0.0/16) provides six private subnets (two per AZ) with NAT egress. Data lives in "
            "Aurora PostgreSQL; secrets in AWS Secrets Manager encrypted by kms-payments-prod.\n\n"
            "Services: orders-api (ECS, 8 tasks), payments (EKS), identity-service. SLOs: p95 latency < 250ms, "
            "error rate < 0.1%. Deploys via GitHub Actions with approval gates for production."
        ),
    },
    {
        "title": "RCA: checkout outage 06-19",
        "source": "rca", "kind": "rca", "uri": "kb://rca/checkout-outage-06-19",
        "content": (
            "Root Cause Analysis — checkout latency, 2026-06-19\n\n"
            "Symptom: checkout p95 latency rose ~12% immediately after the 14:20 deploy. Root cause: a new synchronous "
            "call to the fraud service without a timeout, saturating the connection pool under load.\n\n"
            "Remediation: added a 250ms timeout and a circuit breaker; moved the call off the hot path. Follow-up: "
            "load-test the fraud integration in staging before prod. Correlated with deploy orders-api and the "
            "checkout service change set."
        ),
    },
    {
        "title": "Incident response playbook",
        "source": "sop", "kind": "runbook", "uri": "kb://sop/incident-response",
        "content": (
            "Incident Response Playbook (on-call SOP)\n\n"
            "1. Triage: confirm true vs false positive using metrics (Prometheus) and recent deploys. "
            "2. Declare severity (P1–P4) and open/Link a ServiceNow incident. "
            "3. Mitigate: roll back the correlated deploy or apply the documented remediation after approval. "
            "4. Communicate status to stakeholders. 5. Resolve and publish an RCA; update the relevant runbook."
        ),
    },
]


def _sync_dsn(settings) -> str:
    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"dbname={settings.postgres_db} user={settings.postgres_user} "
        f"password={settings.postgres_password}"
    )


def ensure_extensions(settings) -> None:
    with psycopg.connect(_sync_dsn(settings), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")
    log.info("seed.extensions_ready")


async def _seed_org(session, settings, *, name: str, slug: str, member_count: int,
                    users: list[dict], notifications: list[tuple], documents: list[dict]) -> int:
    """Seed one organization (idempotent). Returns the number of documents ingested."""
    org = (await session.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none()
    if not org:
        org = Organization(name=name, slug=slug, plan="enterprise", member_count=member_count)
        session.add(org)
        await session.flush()
    log.info("seed.org", slug=slug, id=str(org.id))

    # Users
    for u in users:
        existing = (await session.execute(
            select(User).where(User.org_id == org.id, User.username == u["username"])
        )).scalar_one_or_none()
        if existing:
            existing.email, existing.name, existing.roles = u["email"], u["name"], u["roles"]
        else:
            session.add(User(org_id=org.id, username=u["username"], email=u["email"], name=u["name"], roles=u["roles"]))

    # Integrations
    for iname, cat, status in INTEGRATIONS:
        await IntegrationRepo.upsert(session, org.id, name=iname, kind=cat, status=status)

    # Notifications (seed once)
    existing_notifs = (await session.execute(select(Notification).where(Notification.org_id == org.id))).scalars().first()
    if not existing_notifs:
        for title, level, color in notifications:
            session.add(Notification(org_id=org.id, title=title, level=level, color=color))

    # Knowledge documents -> chunks (+embeddings if Gemini configured)
    to_ingest = []
    for d in documents:
        present = (await session.execute(
            select(Document).where(Document.org_id == org.id, Document.title == d["title"])
        )).scalar_one_or_none()
        if not present:
            to_ingest.append(d)
    if to_ingest:
        await ingest.ingest_many(session, org_id=org.id, settings=settings, docs=to_ingest)

    await AuditRepo.log(session, org_id=org.id, actor="seed", action="seed.completed",
                        target=slug, detail={"documents": len(to_ingest), "users": len(users)})
    return len(to_ingest)


async def seed_data(settings) -> None:
    db.init_engine(settings)
    async with db.session_scope() as session:
        # Roles (platform-wide)
        for kebab, display in rbac.ROLE_DISPLAY.items():
            exists = (await session.execute(select(Role).where(Role.name == kebab))).scalar_one_or_none()
            if not exists:
                session.add(Role(name=kebab, display_name=display, description=display))

        ingested = await _seed_org(session, settings, name=ORG_NAME, slug=ORG_SLUG, member_count=184,
                                   users=SEED_USERS, notifications=NOTIFICATIONS, documents=DOCUMENTS)
        ingested += await _seed_org(session, settings, name=ORG_B_NAME, slug=ORG_B_SLUG, member_count=42,
                                    users=ORG_B_USERS, notifications=ORG_B_NOTIFICATIONS,
                                    documents=ORG_B_DOCUMENTS)
    log.info("seed.data_done", documents_ingested=ingested)


def main() -> int:
    configure_logging()
    settings = get_settings()
    log.info("seed.start", db=settings.postgres_db, host=settings.postgres_host)
    try:
        ensure_extensions(settings)
        asyncio.run(seed_data(settings))
    except Exception:
        log.exception("seed.failed")
        return 1
    log.info("seed.done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
