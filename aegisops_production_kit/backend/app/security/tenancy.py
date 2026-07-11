"""Principal → (organization, user) tenancy resolution (S0).

Resolution order (owner-locked decision):
1. The Keycloak ``org`` claim (a group-membership claim carrying the org slug) **wins**
   when present — it is the source of truth for org membership.
2. The ``users`` mirror row is the fallback for seeded users who have never logged in
   through a claim-bearing token: matched by ``keycloak_sub`` first, then by
   username/email for seeded rows that have no sub attached yet.

On every successful resolution the mirror row is upserted — the sub is attached, the
profile (username/email/name/roles) refreshed, and the org moved if the claim says so —
so the ``users`` table converges on Keycloak as the identity source of truth.

A principal that resolves to no organization is refused (strict mode): tenancy is not
optional, and an unscoped request must never fall back to someone else's org.
"""

from __future__ import annotations

from typing import NamedTuple

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Organization, User
from ..logging_conf import get_logger

log = get_logger(__name__)


class TenancyError(Exception):
    """The principal could not be mapped to an organization."""

    def __init__(self, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.status_code = status_code


class Tenancy(NamedTuple):
    org_id: str
    user_id: str
    org_slug: str


async def resolve_tenancy(
    s: AsyncSession,
    *,
    sub: str,
    username: str,
    email: str | None,
    name: str | None,
    roles: list[str],
    org_slug: str | None,
) -> Tenancy:
    """Resolve the authenticated principal to (org_id, user_id, org_slug), upserting the mirror."""
    row = None
    if sub:
        row = (await s.execute(select(User).where(User.keycloak_sub == sub))).scalar_one_or_none()
    if row is None and (username or email):
        # Seeded rows have no keycloak_sub until first login — match by identity fields.
        clauses = [c for c in (
            User.username == username if username else None,
            User.email == email if email else None,
        ) if c is not None]
        row = (await s.execute(
            select(User).where(User.keycloak_sub.is_(None), or_(*clauses)).limit(1)
        )).scalar_one_or_none()

    org: Organization | None = None
    if org_slug:
        org = (await s.execute(select(Organization).where(Organization.slug == org_slug))).scalar_one_or_none()
        if org is None:
            raise TenancyError(f"Unknown organization '{org_slug}' — it is not provisioned on this platform.")
    elif row is not None:
        org = await s.get(Organization, row.org_id)

    if org is None:
        raise TenancyError("Your account has no organization membership on this platform.")

    if row is None:
        row = User(org_id=org.id, keycloak_sub=sub or None, username=username,
                   email=email, name=name, roles=roles)
        try:
            async with s.begin_nested():
                s.add(row)
                await s.flush()
        except IntegrityError:
            # Concurrent first login attached the sub in another transaction — reuse that row.
            row = (await s.execute(select(User).where(User.keycloak_sub == sub))).scalar_one()
    else:
        if sub and row.keycloak_sub is None:
            row.keycloak_sub = sub
        if org_slug and row.org_id != org.id:
            log.info("tenancy.org_moved", user=username, to=org.slug)
            row.org_id = org.id
        row.username, row.email, row.name, row.roles = username, email, name, roles
        await s.flush()

    return Tenancy(org_id=str(org.id), user_id=str(row.id), org_slug=org.slug)
