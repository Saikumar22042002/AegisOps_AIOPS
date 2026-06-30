"""Unit tests for RBAC capability logic."""

from __future__ import annotations

from app.security import rbac


def test_approver_roles() -> None:
    assert rbac.can_approve(["platform-admin"])
    assert rbac.can_approve(["cloud-architect"])
    assert not rbac.can_approve(["developer"])
    assert not rbac.can_approve(["read-only"])


def test_initiator_roles() -> None:
    assert rbac.can_initiate(["devops-engineer"])
    assert rbac.can_initiate(["sre"])
    assert not rbac.can_initiate(["auditor"])
    assert not rbac.can_initiate(["read-only"])


def test_execute_requires_approver() -> None:
    assert rbac.can_execute(["org-admin"])
    assert not rbac.can_execute(["developer"])


def test_display_roles() -> None:
    assert rbac.display_roles(["platform-admin", "sre"]) == ["Platform Admin", "SRE"]
    assert rbac.display_roles(["nonexistent"]) == []
