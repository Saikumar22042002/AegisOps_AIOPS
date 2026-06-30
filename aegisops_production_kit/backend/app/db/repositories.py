"""Org-scoped repository helpers. Every query is scoped to the caller's organization."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog, Integration, Notification, Organization


async def get_default_org(session: AsyncSession) -> Organization | None:
    return (await session.execute(select(Organization).order_by(Organization.created_at).limit(1))).scalar_one_or_none()


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
