"""Secret redaction (6.3) — free-text (logs/console/SSE) and structured (DB/context-graph) paths.

All secrets here are SYNTHETIC (never real credentials): the invariant under test is that the
sensitive value never survives redaction, and non-secret text is left intact.
"""

from __future__ import annotations

import pytest

from app.security.redaction import _MASK, redact, redact_dict

_FAKE_PK = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\nkqhkiG9w0BAQ\n-----END PRIVATE KEY-----"


@pytest.mark.parametrize("secret,text", [
    ("hunter2", "password=hunter2"),
    ("hunter2", "password: hunter2"),
    ("sk-abc123def456", "api_key: sk-abc123def456"),
    ("s3cr3t-value", "client_secret=s3cr3t-value"),
    ("IQoJb3JpZ2luX2VjEExample", "AWS_SESSION_TOKEN=IQoJb3JpZ2luX2VjEExample"),
    ("IQoJsessiontoken", '"SessionToken": "IQoJsessiontoken"'),
    ("wJalrXUtnFEMIK7EXAMPLE", "aws_secret_access_key = wJalrXUtnFEMIK7EXAMPLE"),
    ("AKIAIOSFODNN7EXAMPLE", "access key AKIAIOSFODNN7EXAMPLE leaked"),
    ("ASIAYMN64RPCGPTAGKZI", "temp key ASIAYMN64RPCGPTAGKZI here"),
    ("ghp_0123456789abcdefghij0123456789", "token ghp_0123456789abcdefghij0123456789"),
])
def test_free_text_secret_never_survives(secret, text):
    out = redact(text)
    assert secret not in out, f"secret leaked: {out!r}"
    assert _MASK in out


def test_private_key_body_masked_markers_kept():
    out = redact(_FAKE_PK)
    assert "MIIEvQIBADANBg" not in out
    assert out.startswith("-----BEGIN PRIVATE KEY-----")
    assert out.rstrip().endswith("-----END PRIVATE KEY-----")


def test_jwt_masked():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
    out = redact(f"Authorization: Bearer {jwt}")
    assert jwt not in out and _MASK in out


def test_non_secret_text_untouched():
    for benign in ["instance_type=t3.micro", "region: us-east-1", "location=eastus",
                   "Plan: 1 to add, 0 to change, 0 to destroy", "vpc_id=vpc-0abc123"]:
        assert redact(benign) == benign


def test_redact_dict_masks_by_key_recursively():
    data = {
        "AccessKeyId": "ASIAEXAMPLEKEYID1234",
        "SecretAccessKey": "wJalrXUtnFEMIEXAMPLEKEY",
        "SessionToken": "IQoJexampletoken",
        "region": "us-east-1",
        "nested": {"api_key": "sk-live-xyz", "count": 3},
        "list": [{"password": "pw"}, "plain"],
    }
    out = redact_dict(data)
    assert out["AccessKeyId"] == _MASK
    assert out["SecretAccessKey"] == _MASK
    assert out["SessionToken"] == _MASK
    assert out["region"] == "us-east-1"        # non-secret preserved
    assert out["nested"]["api_key"] == _MASK
    assert out["nested"]["count"] == 3
    assert out["list"][0]["password"] == _MASK
    assert out["list"][1] == "plain"


def test_redact_handles_empty_and_none_safely():
    assert redact("") == ""
    assert redact_dict({}) == {}


# ═══ S4 — persist-time backstop: a secret echoed into answer/outcome never reaches the DB ═══


class TestPersistBackstop:
    async def test_planted_secret_in_answer_and_outcome_is_masked_when_persisted(self, live_db, org_id):
        """S4/P20: even if a future agent echoes a secret into its answer or outcome, the
        persisted messages.content and runs.outcome are redaction-scanned first."""
        import uuid as _uuid

        from sqlalchemy import delete

        from app.api.chat import _persist_result
        from app.db.models import Message, Run, Session
        from app.db.session import session_scope

        async with session_scope() as s:
            sess = Session(org_id=_uuid.UUID(org_id), title="s4")
            s.add(sess)
            await s.flush()
            run = Run(org_id=sess.org_id, session_id=sess.id, status="running", mode="plan")
            s.add(run)
            await s.flush()
            sid, rid = str(sess.id), str(run.id)

        state = {
            "answer": f"Here is your key:\n{_FAKE_PK}\nand password=hunter2 — keep it safe.",
            "outcome": {"status": "applied", "private_key_pem": _FAKE_PK,
                        "detail": {"admin_password": "hunter2"}},
        }
        try:
            msg_id = await _persist_result(rid, sid, org_id, state, "completed")
            async with session_scope() as s:
                msg = await s.get(Message, _uuid.UUID(msg_id))
                run = await s.get(Run, _uuid.UUID(rid))
                assert "MIIEvQIBADANBg" not in msg.content, "private key body leaked into messages.content"
                assert "hunter2" not in msg.content, "password leaked into messages.content"
                flat = str(run.outcome)
                assert "MIIEvQIBADANBg" not in flat, "private key leaked into runs.outcome"
                assert "hunter2" not in flat, "password leaked into runs.outcome"
                assert run.outcome["status"] == "applied"  # non-secret fields preserved
        finally:
            async with session_scope() as s:
                await s.execute(delete(Session).where(Session.id == _uuid.UUID(sid)))
                await s.execute(delete(Run).where(Run.id == _uuid.UUID(rid)))
