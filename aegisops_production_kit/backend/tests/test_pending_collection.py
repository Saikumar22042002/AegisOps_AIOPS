"""Multi-turn parameter collection (6.1) — a pending request is persisted in Redis, keyed by
session, so a follow-up message continues the SAME provisioning request instead of re-routing.
"""

from __future__ import annotations

from app.agents import params


async def test_pending_roundtrip(live_redis):
    sid = "sess-pending-itest"
    await params.clear_pending(sid)
    assert await params.load_pending(sid) is None

    record = {"template": "aws.ec2", "cloud": "aws", "resource": "ec2", "action": "create",
              "collected": {"name": "web-01", "instance_type": "t3.micro"}, "snow_id": "SR001"}
    await params.save_pending(sid, record)

    loaded = await params.load_pending(sid)
    assert loaded["template"] == "aws.ec2"
    assert loaded["cloud"] == "aws"
    assert loaded["collected"] == {"name": "web-01", "instance_type": "t3.micro"}

    await params.clear_pending(sid)
    assert await params.load_pending(sid) is None


async def test_pending_is_session_scoped(live_redis):
    a, b = "sess-A-itest", "sess-B-itest"
    await params.clear_pending(a)
    await params.clear_pending(b)
    await params.save_pending(a, {"template": "aws.s3", "collected": {"bucket_name": "a-bucket"}})
    assert await params.load_pending(b) is None            # sibling session unaffected
    assert (await params.load_pending(a))["template"] == "aws.s3"
    await params.clear_pending(a)
