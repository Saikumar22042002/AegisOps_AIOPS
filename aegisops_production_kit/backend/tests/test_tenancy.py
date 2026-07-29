"""S0 — real multi-tenancy (Phase 1 acceptance).

Two tiers:

* **Unit** — the org-claim normalization and the strict-mode refusal of an unscoped
  principal (no datastore needed; org_for refuses before any query executes).
* **Integration** (`AEGISOPS_TEST_LIVE_DATASTORES=1`) — the tenancy resolver against real
  Postgres (claim wins, mirror updates on login, seeded-user fallback), and full
  endpoint-level isolation between two organizations: sessions, chat, approvals, overview
  and knowledge are invisible across the org boundary (404 on cross-org UUIDs — never an
  enumeration-friendly 403).

The two orgs mirror the seed: northwind-financial and acme-industrial.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas.auth import User as AuthUser
from app.security import rbac
from app.security.deps import _org_claim, get_current_user

ORG_A_SLUG, ORG_A_NAME = "northwind-financial", "Northwind Financial"
ORG_B_SLUG, ORG_B_NAME = "acme-industrial", "Acme Industrial"


def _member(org_id: str | None, user_id: str | None = None, org: str | None = None,
            roles: list[str] | None = None) -> AuthUser:
    roles = roles or ["platform-admin"]
    return AuthUser(sub=f"sub-{user_id or 'x'}", username=f"member-{(user_id or 'x')[:8]}",
                    email="m@example.com", name="Member", roles=roles,
                    display_roles=rbac.display_roles(roles), can_approve=rbac.can_approve(roles),
                    can_initiate=rbac.can_initiate(roles), can_execute=rbac.can_execute(roles),
                    org=org, org_id=org_id, user_id=user_id)


@pytest.fixture
def as_member(client: TestClient):
    """Point the auth dependency at a specific org member for subsequent requests."""
    def _set(user: AuthUser) -> TestClient:
        app.dependency_overrides[get_current_user] = lambda: user
        return client
    yield _set
    app.dependency_overrides.pop(get_current_user, None)


# ═══ Unit — claim normalization + strict refusal ══════════════════════════════════════


def test_org_claim_normalization():
    assert _org_claim({"org": "northwind-financial"}) == "northwind-financial"
    assert _org_claim({"org": ["/northwind-financial"]}) == "northwind-financial"
    assert _org_claim({"org": []}) is None
    assert _org_claim({}) is None
    # Membership in several org groups is ambiguous — never guess.
    assert _org_claim({"org": ["a", "b"]}) is None


def test_strict_mode_refuses_unscoped_principal(as_member):
    """A principal with no resolved org gets 403 from every org-scoped endpoint — it must
    never fall back to someone else's organization (the pre-S0 default-org behavior)."""
    c = as_member(_member(org_id=None))
    assert c.get("/sessions").status_code == 403
    assert c.post("/sessions", json={"title": "x"}).status_code == 403
    assert c.get("/overview").status_code == 403
    assert c.get("/modules/projects").status_code == 403
    assert c.get("/notifications").status_code == 403
    assert c.get("/knowledge/search", params={"q": "eks"}).status_code == 403


# ═══ Integration — resolver + endpoint isolation against real Postgres ════════════════


def _live() -> bool:
    return os.getenv("AEGISOPS_TEST_LIVE_DATASTORES") == "1"


async def _get_or_create_org(s, slug: str, name: str):
    from sqlalchemy import select

    from app.db.models import Organization

    org = (await s.execute(select(Organization).where(Organization.slug == slug))).scalar_one_or_none()
    if not org:
        org = Organization(name=name, slug=slug, plan="enterprise", member_count=1)
        s.add(org)
        await s.flush()
    return org


async def _get_or_create_user(s, org_id, username: str):
    from sqlalchemy import select

    from app.db.models import User

    row = (await s.execute(select(User).where(User.org_id == org_id, User.username == username))).scalar_one_or_none()
    if not row:
        row = User(org_id=org_id, username=username, email=f"{username}@test", name=username,
                   roles=["platform-admin"])
        s.add(row)
        await s.flush()
    return row


class TestTenancyResolver:
    """resolve_tenancy: claim wins · mirror updated on login · seeded-row fallback."""

    async def test_resolver_matrix(self, live_db):
        from app.db.session import session_scope
        from app.security.tenancy import TenancyError, resolve_tenancy

        sub_new = f"sub-{uuid.uuid4()}"
        sub_seeded = f"sub-{uuid.uuid4()}"
        fallback_username = f"fallback-{uuid.uuid4().hex[:8]}"
        created_user_ids = []
        try:
            async with session_scope() as s:
                org_a = await _get_or_create_org(s, ORG_A_SLUG, ORG_A_NAME)
                org_b = await _get_or_create_org(s, ORG_B_SLUG, ORG_B_NAME)
                org_a_id, org_b_id = str(org_a.id), str(org_b.id)

                # 1. Claim present + no mirror row → row created in the claim's org.
                t = await resolve_tenancy(s, sub=sub_new, username=f"claim-{sub_new[:12]}",
                                          email=None, name=None, roles=["developer"],
                                          org_slug=ORG_B_SLUG)
                created_user_ids.append(t.user_id)
                assert t.org_id == org_b_id and t.org_slug == ORG_B_SLUG

                # 2. Claim wins over the mirror: same sub, claim now names org A → row moves.
                t2 = await resolve_tenancy(s, sub=sub_new, username=f"claim-{sub_new[:12]}",
                                           email=None, name=None, roles=["developer"],
                                           org_slug=ORG_A_SLUG)
                assert t2.user_id == t.user_id and t2.org_id == org_a_id

                # 3. No claim → the mirror row is the fallback.
                t3 = await resolve_tenancy(s, sub=sub_new, username=f"claim-{sub_new[:12]}",
                                           email=None, name=None, roles=["developer"], org_slug=None)
                assert t3.org_id == org_a_id

                # 4. Seeded row (no sub yet) matched by username; the sub attaches on login.
                seeded = await _get_or_create_user(s, org_b.id, fallback_username)
                created_user_ids.append(str(seeded.id))
                t4 = await resolve_tenancy(s, sub=sub_seeded, username=fallback_username,
                                           email=None, name=None, roles=["developer"], org_slug=None)
                assert t4.user_id == str(seeded.id) and t4.org_id == org_b_id
                assert seeded.keycloak_sub == sub_seeded

                # 5. Unknown org slug → refused (unknown org must never be auto-provisioned).
                with pytest.raises(TenancyError):
                    await resolve_tenancy(s, sub=f"sub-{uuid.uuid4()}", username="x", email=None,
                                          name=None, roles=[], org_slug="no-such-org")

                # 6. No claim + no mirror row → refused (no membership).
                with pytest.raises(TenancyError):
                    await resolve_tenancy(s, sub=f"sub-{uuid.uuid4()}", username=f"ghost-{uuid.uuid4().hex[:8]}",
                                          email=None, name=None, roles=[], org_slug=None)
        finally:
            from sqlalchemy import delete

            from app.db.models import User
            from app.db.session import session_scope as scope

            async with scope() as s:
                for uid in created_user_ids:
                    await s.execute(delete(User).where(User.id == uuid.UUID(uid)))


@pytest.fixture
def two_orgs(client: TestClient):
    """Org A and org B with one member row each, resolved through the APP's engine (via a
    plain sync bridge) so the TestClient portal loop owns every connection."""
    if not _live():
        pytest.skip("integration test: set AEGISOPS_TEST_LIVE_DATASTORES=1 (run via `make test`)")
    from app.db.session import session_scope

    result: dict = {}

    async def _setup():
        async with session_scope() as s:
            org_a = await _get_or_create_org(s, ORG_A_SLUG, ORG_A_NAME)
            org_b = await _get_or_create_org(s, ORG_B_SLUG, ORG_B_NAME)
            ua = await _get_or_create_user(s, org_a.id, f"tenant-a-{uuid.uuid4().hex[:8]}")
            ub = await _get_or_create_user(s, org_b.id, f"tenant-b-{uuid.uuid4().hex[:8]}")
            result.update(org_a=str(org_a.id), org_b=str(org_b.id),
                          user_a=str(ua.id), user_b=str(ub.id))

    async def _teardown():
        from sqlalchemy import delete

        from app.db.models import Run, Session, User

        async with session_scope() as s:
            for sid in result.get("session_ids", []):
                await s.execute(delete(Session).where(Session.id == uuid.UUID(sid)))
            for rid in result.get("run_ids", []):
                await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
            for uid in (result.get("user_a"), result.get("user_b")):
                if uid:
                    await s.execute(delete(User).where(User.id == uuid.UUID(uid)))

    client.portal.call(_setup)
    result["session_ids"], result["run_ids"] = [], []
    yield result
    client.portal.call(_teardown)


class TestTwoOrgIsolation:
    """The Phase-1 headline: two orgs are fully isolated; cross-org UUIDs are 404."""

    def test_sessions_isolated_and_cross_org_is_404(self, two_orgs, as_member):
        a = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
        b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG)

        r = as_member(a).post("/sessions", json={"title": "org-A secret plan"})
        assert r.status_code == 200
        sid = r.json()["id"]
        two_orgs["session_ids"].append(sid)

        assert sid in [x["id"] for x in as_member(a).get("/sessions").json()["sessions"]]

        c = as_member(b)
        assert sid not in [x["id"] for x in c.get("/sessions").json()["sessions"]]
        assert c.patch(f"/sessions/{sid}", json={"title": "hijack"}).status_code == 404
        assert c.delete(f"/sessions/{sid}").status_code == 404
        assert c.post(f"/sessions/{sid}/close").status_code == 404
        # Continuing someone else's conversation from /chat is refused before any run starts.
        assert c.post("/chat", json={"sessionId": sid, "message": "hello"}).status_code == 404

        assert as_member(a).delete(f"/sessions/{sid}").status_code == 200
        two_orgs["session_ids"].remove(sid)

    def test_session_owner_recorded(self, two_orgs, as_member, client):
        a = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
        r = as_member(a).post("/sessions", json={"title": "ownership"})
        assert r.status_code == 200
        sid = r.json()["id"]
        two_orgs["session_ids"].append(sid)

        async def _owner():
            from app.db.models import Session
            from app.db.session import session_scope

            async with session_scope() as s:
                row = await s.get(Session, uuid.UUID(sid))
                return str(row.user_id) if row and row.user_id else None

        assert client.portal.call(_owner) == two_orgs["user_a"], \
            "Session.user_id must record the creating principal (S0)"

    def test_cross_org_approval_is_404(self, two_orgs, as_member, client):
        async def _mk_run():
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(two_orgs["org_a"]), status="awaiting_approval", mode="apply")
                s.add(run)
                await s.flush()
                return str(run.id)

        rid = client.portal.call(_mk_run)
        two_orgs["run_ids"].append(rid)

        b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG)  # approver role
        r = as_member(b).post(f"/approvals/{rid}", json={"decision": "approved"})
        assert r.status_code == 404, "a run outside the approver's org must not exist for them"

    def test_double_approval_endpoint_guard(self, two_orgs, as_member, client):
        """A1 endpoint guard: while one /approvals is being driven (the run stays
        `awaiting_approval` in the DB until it finishes), a second /approvals for the same run
        is refused with 409 — the NX in-flight lock closes the double-click window."""
        async def _mk_awaiting():
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(two_orgs["org_a"]), status="awaiting_approval",
                          mode="apply", env="Staging")
                s.add(run)
                await s.flush()
                return str(run.id)

        rid = client.portal.call(_mk_awaiting)
        two_orgs["run_ids"].append(rid)

        async def _set_lock():
            from app.cache.redis import get_redis
            await get_redis().set(f"approval:inflight:{rid}", "peer", nx=True, ex=900)

        async def _clear_lock():
            from app.cache.redis import get_redis
            await get_redis().delete(f"approval:inflight:{rid}")

        client.portal.call(_set_lock)
        try:
            approver = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
            r = as_member(approver).post(f"/approvals/{rid}", json={"decision": "approved"})
            assert r.status_code == 409 and "already being processed" in r.json()["detail"].lower()
        finally:
            client.portal.call(_clear_lock)

    def test_overview_is_org_scoped(self, two_orgs, as_member):
        a = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
        b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG)
        assert as_member(a).get("/overview").json()["org"]["name"] == ORG_A_NAME
        assert as_member(b).get("/overview").json()["org"]["name"] == ORG_B_NAME

    def test_cross_org_read_of_every_tab_is_404(self, two_orgs, as_member, client):
        """S2: authorize_run/authorize_session on every read/stream endpoint — a run or
        session in org A does not exist for org B, on any tab, ever."""
        async def _mk():
            from app.db.models import Message, Run, Session
            from app.db.session import session_scope

            async with session_scope() as s:
                sess = Session(org_id=uuid.UUID(two_orgs["org_a"]), title="s2")
                s.add(sess)
                await s.flush()
                s.add(Message(org_id=sess.org_id, session_id=sess.id, role="user", content="hi"))
                run = Run(org_id=sess.org_id, session_id=sess.id, status="completed", mode="plan")
                s.add(run)
                await s.flush()
                return str(sess.id), str(run.id)

        sid, rid = client.portal.call(_mk)
        two_orgs["session_ids"].append(sid)
        two_orgs["run_ids"].append(rid)

        a = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
        b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG)

        run_reads = [f"/runs/{rid}"] + [
            f"/runs/{rid}/{tab}" for tab in
            ("timeline", "reasoning", "terraform", "logs", "metrics", "traces", "references", "approvals")
        ]
        c = as_member(b)
        for url in run_reads:
            assert c.get(url).status_code == 404, f"cross-org {url} must be 404"
        assert c.get(f"/chat/stream/{rid}").status_code == 404
        assert c.get(f"/sessions/{sid}/messages").status_code == 404
        assert c.post(f"/runs/{rid}/credentials", json={"output": "private_key_pem"}).status_code == 404

        # The owner org still reads everything (stream 404s only because no LIVE channel exists).
        c = as_member(a)
        for url in run_reads:
            assert c.get(url).status_code == 200, f"same-org {url} must be readable"
        assert c.get(f"/sessions/{sid}/messages").status_code == 200
        # Invalid UUIDs are 404, not 500.
        assert c.get("/runs/not-a-uuid/timeline").status_code == 404
        assert c.get("/sessions/not-a-uuid/messages").status_code == 404

    def test_four_eyes_blocks_prod_self_approval(self, two_orgs, as_member, client, monkeypatch):
        """A5: the initiator of a Production run cannot approve it; a different approver
        passes the 4-eyes gate; non-Production runs are exempt. (Runs are created in a
        non-awaiting status so the gate is exercised without driving the graph.)

        The flag is PINNED on for the duration of this test. Without pinning, the assertion
        depends on the operator's `.env`: an install with AEGISOPS_FOUR_EYES_FOR_PRODUCTION=false
        makes this test fail (409 past the gate instead of 403 at it) even though the code is
        correct — and, worse, silently stops covering A5 at all wherever the flag is off. The
        gate's behaviour is what we assert here; whether a given deployment enables it is a
        deployment decision, tested by the settings default.
        """
        from app.settings import get_settings

        monkeypatch.setattr(get_settings(), "aegisops_four_eyes_for_production", True)

        async def _mk(env: str):
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(two_orgs["org_a"]), status="completed", mode="apply",
                          env=env, initiated_by=uuid.UUID(two_orgs["user_a"]))
                s.add(run)
                await s.flush()
                return str(run.id)

        prod = client.portal.call(_mk, "Production")
        staging = client.portal.call(_mk, "Staging")
        two_orgs["run_ids"] += [prod, staging]

        initiator = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)  # approver caps
        other = _member(two_orgs["org_a"], str(uuid.uuid4()), ORG_A_SLUG)  # different approver

        r = as_member(initiator).post(f"/approvals/{prod}", json={"decision": "approved"})
        assert r.status_code == 403 and "four-eyes" in r.json()["detail"].lower(), \
            "prod self-approval must be refused by the 4-eyes policy"
        # A different approver passes 4-eyes (409 = past the gate: run isn't awaiting approval).
        assert as_member(other).post(f"/approvals/{prod}", json={"decision": "approved"}).status_code == 409
        # Non-production: the initiator may approve (subject to role policy) — gate exempt.
        assert as_member(initiator).post(f"/approvals/{staging}", json={"decision": "approved"}).status_code == 409

    def test_knowledge_search_is_org_scoped(self, two_orgs, as_member):
        a = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG)
        b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG)
        ra = as_member(a).get("/knowledge/search", params={"q": "EKS production hardening"})
        assert ra.status_code == 200
        if not ra.json()["results"]:
            pytest.skip("knowledge corpus not seeded in this environment; run `make seed`")
        rb = as_member(b).get("/knowledge/search", params={"q": "EKS production hardening"})
        assert rb.status_code == 200
        titles_b = [x.get("title", "") for x in rb.json()["results"]]
        assert not any("EKS" in t for t in titles_b), \
            "org B must never see org A's knowledge documents"


class TestTerminalStateB5:
    """B5: a run always reaches a terminal state — even if the persist path itself throws, the
    drive's except backstop force-marks it failed rather than leaving it stuck in `running`."""

    def test_force_terminal_marks_running_run_failed(self, two_orgs, client):
        async def _run():
            from app.api.chat import _force_terminal
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(two_orgs["org_a"]), status="running", mode="apply")
                s.add(run)
                await s.flush()
                rid = str(run.id)
            await _force_terminal(rid, "boom in the driver")
            async with session_scope() as s:
                row = await s.get(Run, uuid.UUID(rid))
                return rid, row.status, (row.outcome or {}).get("status"), (row.outcome or {}).get("error")

        rid, status, oc_status, err = client.portal.call(_run)
        two_orgs["run_ids"].append(rid)
        assert status == "failed" and oc_status == "failed"
        assert "boom" in (err or "")

    def test_force_terminal_leaves_terminal_run_untouched(self, two_orgs, client):
        async def _run():
            from app.api.chat import _force_terminal
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(two_orgs["org_a"]), status="completed", mode="apply",
                          outcome={"status": "applied", "outputs": {"id": "keep"}})
                s.add(run)
                await s.flush()
                rid = str(run.id)
            await _force_terminal(rid, "should not overwrite a completed run")
            async with session_scope() as s:
                row = await s.get(Run, uuid.UUID(rid))
                return rid, row.status, row.outcome
        rid, status, outcome = client.portal.call(_run)
        two_orgs["run_ids"].append(rid)
        assert status == "completed" and outcome["status"] == "applied", \
            "a run that already reached a terminal state must not be clobbered"

    def test_chat_persist_failure_still_ends_failed(self, two_orgs, as_member, client, monkeypatch):
        """Fault injection: raise inside _persist_result → the run ends `failed`, never `running`."""
        from app.api import chat as chat_api

        async def _fake_graph(run_id, channel, initial=None, resume=None):
            return {"state": {"answer": "ok", "domain": "general"}, "interrupted": False, "error": None}

        async def _boom(*a, **k):
            raise RuntimeError("planted persist failure")

        monkeypatch.setattr(chat_api, "run_graph", _fake_graph)
        monkeypatch.setattr(chat_api, "_persist_result", _boom)

        member = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG, roles=["developer"])
        body = {"message": "hello there", "context": {"env": "Staging"}}
        with as_member(member).stream("POST", "/chat", json=body) as resp:
            assert resp.status_code == 200
            stream = "".join(resp.iter_text())  # drain until the channel closes (drive finished)
        run_id = None
        for line in stream.splitlines():
            if line.startswith("data:") and '"runId"' in line:
                import json as _json
                run_id = _json.loads(line[5:].strip()).get("runId")
                break
        assert run_id, "no runId surfaced on the stream"
        two_orgs["run_ids"].append(run_id)

        async def _status():
            from app.db.models import Run
            from app.db.session import session_scope
            async with session_scope() as s:
                row = await s.get(Run, uuid.UUID(run_id))
                return row.status
        assert client.portal.call(_status) == "failed", "a persist failure must leave the run failed, not running"


class TestCredentialRevealS1:
    """S1: reveal is initiator-or-approver + org-scoped + step-up re-auth + always-on audit.

    verify_stepup_auth (real Keycloak password grant) and the Terraform read are stubbed so the
    authorization / freshness-gating / one-shot / audit contract is exercised deterministically
    without live Keycloak or Terraform state."""

    def _mk_run(self, client, org_id: str, initiator_id: str) -> str:
        async def _c():
            from app.db.models import Run
            from app.db.session import session_scope

            async with session_scope() as s:
                run = Run(org_id=uuid.UUID(org_id), status="completed", mode="apply", env="Staging",
                          initiated_by=uuid.UUID(initiator_id),
                          plan_json={"workspace": "demo-null", "state_workspace": None},
                          outcome={"status": "applied", "sensitive_outputs": ["private_key_pem"]})
                s.add(run)
                await s.flush()
                return str(run.id)
        return client.portal.call(_c)

    def _audit_count(self, client, run_id: str) -> int:
        async def _c():
            from sqlalchemy import func, select

            from app.db.models import AuditLog
            from app.db.session import session_scope

            async with session_scope() as s:
                return (await s.execute(
                    select(func.count()).select_from(AuditLog)
                    .where(AuditLog.action == "credential.reveal",
                           AuditLog.target == f"run:{run_id}/private_key_pem")
                )).scalar_one()
        return client.portal.call(_c)

    def _cleanup(self, client, run_id: str):
        async def _c():
            from sqlalchemy import delete

            from app.db.models import AuditLog, Run
            from app.db.session import session_scope

            async with session_scope() as s:
                await s.execute(delete(AuditLog).where(
                    AuditLog.target == f"run:{run_id}/private_key_pem"))
                await s.execute(delete(Run).where(Run.id == uuid.UUID(run_id)))
        client.portal.call(_c)

    def test_reveal_authz_stepup_oneshot_and_audit(self, two_orgs, as_member, client, monkeypatch):
        from app.api import artifacts as art

        # Fresh-auth proof is valid iff password == "goodpass"; Terraform read is stubbed.
        async def _fake_stepup(user, password, settings):
            return password == "goodpass"

        async def _fake_output_raw(self, name):
            return "-----BEGIN PRIVATE KEY-----\nSTUBBEDVALUE\n-----END PRIVATE KEY-----"

        monkeypatch.setattr(art, "verify_stepup_auth", _fake_stepup)
        from app.tools.terraform import TerraformRunner
        monkeypatch.setattr(TerraformRunner, "output_raw", _fake_output_raw)

        rid = self._mk_run(client, two_orgs["org_a"], two_orgs["user_a"])
        try:
            initiator = _member(two_orgs["org_a"], two_orgs["user_a"], ORG_A_SLUG, roles=["developer"])
            approver = _member(two_orgs["org_a"], str(uuid.uuid4()), ORG_A_SLUG, roles=["cloud-architect"])
            outsider = _member(two_orgs["org_a"], str(uuid.uuid4()), ORG_A_SLUG, roles=["developer"])
            org_b = _member(two_orgs["org_b"], two_orgs["user_b"], ORG_B_SLUG, roles=["org-admin"])
            body = {"output": "private_key_pem"}

            attempts = 0

            # Cross-org caller → 404 (no enumeration), still audited.
            assert as_member(org_b).post(f"/runs/{rid}/credentials", json=body).status_code == 404
            attempts += 1
            # Non-initiator, non-approver → 404, audited.
            assert as_member(outsider).post(f"/runs/{rid}/credentials", json=body).status_code == 404
            attempts += 1
            # Initiator without a fresh proof → 401, audited.
            assert as_member(initiator).post(f"/runs/{rid}/credentials", json=body).status_code == 401
            attempts += 1
            # Initiator with a bad password → 401, audited.
            assert as_member(initiator).post(f"/runs/{rid}/credentials",
                                             json={**body, "password": "wrong"}).status_code == 401
            attempts += 1
            # Approver (not the initiator) with a fresh proof → value once.
            r = as_member(approver).post(f"/runs/{rid}/credentials", json={**body, "password": "goodpass"})
            assert r.status_code == 200 and "STUBBEDVALUE" in r.json()["value"]
            attempts += 1
            # Second reveal → 410, audited.
            assert as_member(approver).post(f"/runs/{rid}/credentials",
                                            json={**body, "password": "goodpass"}).status_code == 410
            attempts += 1

            assert self._audit_count(client, rid) == attempts, \
                "every reveal attempt (success and denial) must write an audit row"
        finally:
            async def _clear():
                from app.cache.redis import get_redis
                await get_redis().delete(f"reveal:{rid}:private_key_pem")
            client.portal.call(_clear)
            self._cleanup(client, rid)
