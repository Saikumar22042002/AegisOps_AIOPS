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
