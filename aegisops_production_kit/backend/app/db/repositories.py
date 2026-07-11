"""Org-scoped repository helpers. Every query is scoped to the caller's organization."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..settings import get_settings
from .models import AuditLog, Integration, Notification, Organization


async def org_for(session: AsyncSession, user: Any) -> Organization:
    """The authenticated principal's organization (S0). This is the ONLY way an endpoint
    may resolve an org — never a platform-wide default. In `legacy` tenancy mode (rollback
    flag) an unresolved principal falls back to the oldest org, preserving pre-S0 behavior."""
    org_id = getattr(user, "org_id", None)
    if org_id:
        org = await session.get(Organization, uuid.UUID(org_id))
        if org:
            return org
    if get_settings().aegisops_tenancy == "legacy":
        org = (await session.execute(
            select(Organization).order_by(Organization.created_at).limit(1)
        )).scalar_one_or_none()
        if org:
            return org
        raise HTTPException(500, "No organization seeded; run `make seed`.")
    raise HTTPException(403, "Your account has no organization membership on this platform.")


async def get_org_by_slug(session: AsyncSession, slug: str) -> Organization | None:
    return (await session.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none()


class IntegrationRepo:
    @staticmethod
    async def list(session: AsyncSession, org_id: uuid.UUID) -> list[Integration]:
        return list((await session.execute(select(Integration).where(Integration.org_id == org_id))).scalars())

    @staticmethod
    async def upsert(session: AsyncSession, org_id: uuid.UUID, name: str, kind: str,
                     status: str = "unknown", config_ref: str | None = None) -> Integration:
        existing = (await session.execute(
            select(Integration).where(Integration.org_id == org_id, Integration.name == name)
        )).scalar_one_or_none()
        if existing:
            existing.kind = kind
            existing.status = status
            existing.config_ref = config_ref
            return existing
        obj = Integration(org_id=org_id, name=name, kind=kind, status=status, config_ref=config_ref)
        session.add(obj)
        return obj


class AuditRepo:
    @staticmethod
    async def log(session: AsyncSession, *, org_id: uuid.UUID | None, actor: str, action: str,
                  target: str | None = None, detail: dict[str, Any] | None = None,
                  correlation: dict[str, Any] | None = None) -> AuditLog:
        entry = AuditLog(org_id=org_id, actor=actor, action=action, target=target,
                         detail=detail, correlation=correlation)
        session.add(entry)
        return entry


class NotificationRepo:
    @staticmethod
    async def list(session: AsyncSession, org_id: uuid.UUID, limit: int = 50) -> list[Notification]:
        return list((await session.execute(
            select(Notification).where(Notification.org_id == org_id)
            .order_by(Notification.created_at.desc()).limit(limit)
        )).scalars())

    @staticmethod
    async def create(session: AsyncSession, org_id: uuid.UUID, title: str, level: str = "info",
                     color: str | None = None, body: str | None = None) -> Notification:
        obj = Notification(org_id=org_id, title=title, level=level, color=color, body=body)
        session.add(obj)
        return obj
