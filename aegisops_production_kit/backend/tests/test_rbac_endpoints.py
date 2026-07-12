"""RBAC + input guards at side-effecting endpoints (6.3).

Enforcement must live at the API boundary, not just the UI. Unauthenticated callers are rejected
(401) before any datastore is touched; authenticated non-approvers cannot resolve an approval
(403); and the approval decision is validated (400) before the run is looked up. The pure
role/capability logic is covered in test_rbac.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.auth import User
from app.security import rbac
from app.security.deps import get_current_user

_RUN = "00000000-0000-0000-0000-000000000000"


def _user(roles: list[str]) -> User:
    return User(sub="t", username="tester", email="t@example.com", name="Tester", roles=roles,
                display_roles=rbac.display_roles(roles), can_approve=rbac.can_approve(roles),
                can_initiate=rbac.can_initiate(roles), can_execute=rbac.can_execute(roles), org="acme")


@pytest.fixture
def as_user(client: TestClient):
    """Override the auth dependency to a user with the given roles for one request."""
    def _set(roles: list[str]):
        app.dependency_overrides[get_current_user] = lambda: _user(roles)
        return client
    yield _set
    app.dependency_overrides.pop(get_current_user, None)


def test_chat_requires_auth(client: TestClient):
    assert client.post("/chat", json={"message": "create an s3 bucket"}).status_code == 401


def test_chat_requires_initiator(as_user):
    """S3: read-only roles cannot initiate a run — `require_initiator` rejects them at the
    RBAC boundary, before any datastore is touched. Initiator roles pass that gate (they may
    still fail later on org resolution / DB, but never with the initiator-gate message)."""
    for role in ("read-only", "auditor"):
        r = as_user([role]).post("/chat", json={"message": "create an s3 bucket"})
        assert r.status_code == 403, f"{role} must not initiate a run"
        assert "initiate" in r.json()["detail"].lower()
    for role in ("developer", "sre", "devops-engineer", "cloud-architect", "platform-admin"):
        r = as_user([role]).post("/chat", json={"message": "create an s3 bucket"})
        # Passed the initiator gate: any later failure must NOT be the initiator-gate 403.
        assert not (r.status_code == 403 and "initiate" in r.json().get("detail", "").lower()), \
            f"{role} was wrongly blocked by the initiator gate"


def test_approvals_requires_auth(client: TestClient):
    assert client.post(f"/approvals/{_RUN}", json={"decision": "approved"}).status_code == 401


def test_midrun_input_endpoint_removed(client: TestClient):
    """U5: mid-run stdin input was an unwired stub (producer with no consumer, no UI). It was
    removed, not wired — the route no longer exists (404, before any auth dependency)."""
    assert client.post(f"/runs/{_RUN}/input", json={"value": "x"}).status_code == 404


def test_command_console_has_no_stdin_injection():
    """U5: the orphaned interactive-input surface (send_input) is gone from the console tool."""
    from app.tools.console import CommandConsole
    assert not hasattr(CommandConsole, "send_input")


def test_approvals_forbidden_for_non_approver(as_user):
    for role in ("developer", "read-only", "auditor", "sre", "devops-engineer"):
        r = as_user([role]).post(f"/approvals/{_RUN}", json={"decision": "approved"})
        assert r.status_code == 403, f"{role} must not approve"


def test_approvals_bad_decision_rejected_before_lookup(as_user):
    # An approver passes the RBAC gate; an invalid decision is a 400 (validated before DB).
    r = as_user(["platform-admin"]).post(f"/approvals/{_RUN}", json={"decision": "maybe"})
    assert r.status_code == 400


def test_approver_roles_pass_the_gate(as_user):
    # A valid approver + valid decision passes RBAC + validation and reaches the run lookup,
    # which 404s for this non-existent run (i.e. NOT 401/403/400 — the gate let it through).
    r = as_user(["cloud-architect"]).post(f"/approvals/{_RUN}", json={"decision": "approved"})
    assert r.status_code in (404, 409, 500)  # past the guards; run doesn't exist
    assert r.status_code not in (401, 403, 400)
