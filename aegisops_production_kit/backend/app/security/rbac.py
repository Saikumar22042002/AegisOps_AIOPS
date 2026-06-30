"""Role-based access control — pure role/capability logic (no FastAPI imports).

Roles come from Keycloak realm roles (kebab-case). The FastAPI dependency guards that
enforce these live in `security/deps.py` (kept separate to avoid an import cycle with the
auth router). RBAC is enforced at the API boundary AND re-checked at every side-effecting
tool in the agent graph; the UI mirrors the same capabilities for affordances only.

Capability tiers (01_REQUIREMENTS §2):
  approve/apply  -> platform-admin, org-admin, cloud-architect
  initiate only  -> devops-engineer, sre, developer
  read-only      -> auditor, read-only
"""

from __future__ import annotations

# kebab realm role -> UI display label (matches the design's role selector).
ROLE_DISPLAY: dict[str, str] = {
    "platform-admin": "Platform Admin",
    "org-admin": "Org Admin",
    "cloud-architect": "Cloud Architect",
    "devops-engineer": "DevOps Engineer",
    "sre": "SRE",
    "developer": "Developer",
    "auditor": "Auditor",
    "read-only": "Read Only",
}

APPROVER_ROLES = {"platform-admin", "org-admin", "cloud-architect"}
INITIATOR_ROLES = APPROVER_ROLES | {"devops-engineer", "sre", "developer"}
READONLY_ROLES = {"auditor", "read-only"}
ALL_ROLES = set(ROLE_DISPLAY)


def display_roles(roles: list[str]) -> list[str]:
    return [ROLE_DISPLAY[r] for r in roles if r in ROLE_DISPLAY]


def can_approve(roles: list[str]) -> bool:
    return any(r in APPROVER_ROLES for r in roles)


def can_initiate(roles: list[str]) -> bool:
    return any(r in INITIATOR_ROLES for r in roles)


def can_execute(roles: list[str]) -> bool:
    # Side-effecting execution (apply/destroy) requires an approver role.
    return can_approve(roles)
