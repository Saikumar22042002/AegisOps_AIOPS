"""Idempotency keys (6.3) — the same tool execution never runs twice on retry/resume.

`make_key` is pure (unit); the claim/store/release lifecycle uses live Redis (integration).
"""

from __future__ import annotations

from app.security import idempotency


def test_make_key_stable_and_distinct():
    a = idempotency.make_key("tf-exec", "run-1", "apply")
    b = idempotency.make_key("tf-exec", "run-1", "apply")
    c = idempotency.make_key("tf-exec", "run-1", "destroy")
    d = idempotency.make_key("tf-exec", "run-2", "apply")
    assert a == b               # deterministic
    assert a != c and a != d    # distinct per (run, mode)
    assert a.startswith("idem:")


async def test_claim_blocks_duplicate_then_release_reenables(live_redis):
    key = idempotency.make_key("tf-exec", "run-itest", "apply")
    await idempotency.release(key)                       # clean slate
    assert await idempotency.claim(key) is True          # first claim wins
    assert await idempotency.claim(key) is False         # duplicate blocked (no second apply)
    assert await idempotency.get_result(key) is None     # in-progress → not "done"
    await idempotency.store_result(key, {"status": "applied", "outputs": {"id": "i-x"}})
    got = await idempotency.get_result(key)
    assert got["result"]["status"] == "applied"
    await idempotency.release(key)
    assert await idempotency.claim(key) is True          # released → a later retry may re-run
    await idempotency.release(key)


# ═══ A1/B7 — wait-or-abort: an in-flight claim NEVER falls through to a second apply ═══════


async def test_in_progress_helper_and_wait_returns_stored_result(live_redis):
    key = idempotency.make_key("tf-exec", "run-a1-a", "apply")
    await idempotency.release(key)
    assert await idempotency.claim(key) is True
    assert await idempotency.is_in_progress(key) is True     # claimed, no result yet
    await idempotency.store_result(key, {"status": "applied", "outputs": {"id": "i-1"}})
    assert await idempotency.is_in_progress(key) is False     # done, not in-progress
    got = await idempotency.wait_for_result(key, deadline_s=1)
    assert got and got["result"]["status"] == "applied"
    await idempotency.release(key)


async def test_wait_returns_none_when_claim_released_by_failed_peer(live_redis):
    """A peer that failed releases its claim → wait returns None fast (no result will arrive),
    and the caller must abort rather than execute."""
    key = idempotency.make_key("tf-exec", "run-a1-b", "apply")
    await idempotency.release(key)
    assert await idempotency.wait_for_result(key, deadline_s=1) is None


async def test_wait_aborts_when_still_in_progress_at_deadline(live_redis):
    """Claim held, never resolved → wait times out to None → caller ABORTS (never applies)."""
    key = idempotency.make_key("tf-exec", "run-a1-c", "apply")
    await idempotency.release(key)
    assert await idempotency.claim(key) is True
    got = await idempotency.wait_for_result(key, deadline_s=0.8, interval_s=0.2)
    assert got is None                                        # abort signal, not a fall-through
    assert await idempotency.is_in_progress(key) is True      # still held; nothing double-applied
    await idempotency.release(key)


async def test_cloudops_execute_aborts_on_in_flight_claim(live_redis, monkeypatch):
    """The A1 contract at the node: a second cloudops_execute for a run whose apply is
    in-flight returns an *aborted* outcome — it must NOT reach runner.apply()."""
    from app.agents import cloudops
    from app.agents.events import Emitter, RunChannel
    from app.agents import templates

    run_id = "run-a1-node"
    mode = "apply"
    key = idempotency.make_key("tf-exec", run_id, mode)
    await idempotency.release(key)
    assert await idempotency.claim(key) is True  # simulate a peer already applying

    # If apply() were ever reached this test would fail loudly rather than silently pass.
    def _boom(*a, **k):
        raise AssertionError("runner.apply must NOT run while a claim is in flight (A1)")

    monkeypatch.setattr(cloudops.TerraformRunner, "apply", _boom, raising=False)

    tmpl = templates.select("aws", "s3")
    state = {"run_id": run_id, "approval_status": "approved", "execution_mode": mode,
             "cloud": "aws", "resource": "s3", "org_id": "00000000-0000-0000-0000-000000000000",
             "state_workspace": None, "plan_json": {"summary": {"add": 1}},
             "parsed_inputs": {}}
    cfg = {"configurable": {"emitter": Emitter(RunChannel(run_id))}}
    out = await cloudops.cloudops_execute(state, cfg)
    assert out["outcome"]["status"] == f"{mode}_aborted"
    await idempotency.release(key)
