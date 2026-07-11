"""Phase-A RUN LIFECYCLE (03_TEST_MATRIX §E — guards N-01, and N-07's duplication rule).

Screenshots 4/5/19: apply runs hang forever on the Verification spinner. Invariants:
  • verify() terminates within a bounded time even when a cloud SDK stalls;
  • a completed/failed run's timeline never reports elapsed "running";
  • finalize's timeline resolution is a short status — not a verbatim copy of a long answer.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.agents.events import Emitter, RunChannel
from app.agents.finalize import finalize, verify


def _cfg():
    return {"configurable": {"emitter": Emitter(RunChannel("lc-run"))}}


def _state(**kw):
    base = {"run_id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "cloud": "aws",
            "message": "test", "outcome": {}}
    base.update(kw)
    return base


class TestVerifyTerminates:
    async def test_verify_completes_fast_on_applied_outcome(self):
        state = _state(outcome={"status": "applied", "outputs": {"bucket_name": "b1"}})
        out = await asyncio.wait_for(verify(state, _cfg()), timeout=30)
        assert out  # returned, did not hang

    async def test_verify_skips_non_apply_outcomes(self):
        out = await asyncio.wait_for(verify(_state(outcome={"status": "plan_failed"}), _cfg()), timeout=5)
        assert out == {}

    async def test_verify_bounded_when_cloud_sdk_stalls(self, monkeypatch):
        """The N-01 class: a hanging SDK call must yield a warned/failed verification within
        the timeout — never an infinite spinner."""
        from app.agents import finalize as fz
        from app.tools import aws as aws_tool

        class _Hang:
            enabled = True

            async def list_instances(self, *a, **k):
                await asyncio.sleep(3600)

            async def list_buckets(self, *a, **k):
                await asyncio.sleep(3600)

            def __getattr__(self, name):
                async def _stall(*a, **k):
                    await asyncio.sleep(3600)
                return _stall

        monkeypatch.setattr(aws_tool, "get_aws", lambda s: _Hang())
        monkeypatch.setattr(fz.aws_tool, "get_aws", lambda s: _Hang(), raising=False)
        state = _state(outcome={"status": "applied",
                                "outputs": {"instance_id": "i-hang", "public_ip": "203.0.113.9",
                                            "login_user": "ubuntu", "public_dns": "h", "key_name": "k"}})
        # Must resolve on its own well under a minute (verify's own timeout), not ours.
        out = await asyncio.wait_for(verify(state, _cfg()), timeout=45)
        checks = next((tr["verify"] for tr in out.get("tool_results", []) if "verify" in tr), [])
        assert checks, "verify must record its checks even when the SDK stalls"


class TestFinalizeResolution:
    async def test_finalize_resolution_is_not_a_verbatim_copy_of_a_long_answer(self):
        # N-07: the timeline Finalize node duplicated the whole chat bubble (screens 15/16/18).
        long_answer = "Here is a very long structured operational answer. " * 30
        state = _state(answer=long_answer, approval_status="not_required")
        out = await finalize(state, _cfg())
        assert out["resolution"] != long_answer
        assert len(out["resolution"]) <= 200

    async def test_finalize_short_answers_still_meaningful(self):
        state = _state(answer="3 running instances.", approval_status="not_required")
        out = await finalize(state, _cfg())
        assert out["resolution"]  # something human-readable

    async def test_failed_outcome_is_terminal_with_reason(self):
        state = _state(outcome={"status": "apply_failed",
                                "failure": {"title": "The Azure service principal doesn't have permission"}})
        out = await finalize(state, _cfg())
        assert out["resolution"].lower().startswith("failed")


class TestTimelineNeverStuckRunning(object):
    """DB-level: a run persisted as completed/failed must not render elapsed 'running'."""

    async def test_completed_run_timeline_is_terminal(self, live_db, org_id):
        from app.api.artifacts import timeline as timeline_ep
        from app.db.models import Run
        from app.db.session import session_scope
        from app.schemas.auth import User as AuthUser

        async with session_scope() as s:
            run = Run(org_id=uuid.UUID(org_id), status="completed", mode="apply",
                      domain="cloudops", intent="create_s3", outcome={"status": "applied"})
            s.add(run)
            await s.flush()
            rid = str(run.id)
        viewer = AuthUser(sub="t", username="viewer", org_id=org_id)  # S2: reads are org-scoped
        try:
            data = await timeline_ep(rid, user=viewer)
            assert data["elapsed"] != "running"
            ver = [n for n in data["nodes"] if n["title"] == "Verification"]
            assert all(n["status"] in ("done", "failed") for n in ver)
        finally:
            from sqlalchemy import delete
            async with session_scope() as s:
                await s.execute(delete(Run).where(Run.id == uuid.UUID(rid)))
