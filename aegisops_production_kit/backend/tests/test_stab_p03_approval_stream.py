"""STAB P0-3 — the approval continuation stream tails from NOW on the redis bus.

Live (2026-07-20, raw-SSE diagnostic during the two-user approve): the run's redis stream
still holds the ORIGINAL turn's frames ending in ITS __eos__ marker, and /approvals
consumed from cursor 0 — the browser replayed the plan turn, stopped at the old marker,
and NEVER received the apply progress or `done`. The apply completed server-side; the UI
sat on "applying" forever — the exact "approve then silence" the owner reported. Memory-
mode channels are created fresh, which is why the N-01-era tests never caught it.

The fix: `current_cursor()` captured BEFORE the continuation drive starts; the response
consumes `replay_after=cursor`.
"""

from __future__ import annotations

import uuid

from app.agents.events import DONE, Emitter, RedisChannel, RunChannel


async def _drain(channel, replay="0", limit=50):
    channel.replay_after(replay)
    out = []
    for _ in range(limit):
        item = await channel.queue.get()
        if item is DONE:
            break
        out.append(item)
    return out


async def _cleanup(run_id: str):
    from app.cache.redis import get_redis
    await get_redis().delete(f"run:{run_id}:events")


async def _seed_original_turn(run_id: str) -> None:
    """The plan turn as the live stream held it: frames, interrupt, then ITS EOS."""
    ch = RedisChannel(run_id)
    em = Emitter(ch)
    await em.run({"runId": run_id, "sessionId": "s-1"})
    await em.step(4, "Ran terraform plan")
    await em.interrupt({"runId": run_id, "workflow": "aws.s3"})
    await ch.close()  # the ORIGINAL turn's __eos__ — the marker that ate the continuation


async def test_continuation_tails_past_the_original_turns_eos(live_redis):
    run_id = f"itest-p03-{uuid.uuid4()}"
    try:
        await _seed_original_turn(run_id)

        # resolve_approval's shape: fresh channel, cursor BEFORE the drive, then the drive
        # publishes the continuation and closes.
        cont = RedisChannel(run_id)
        cursor = await cont.current_cursor()
        em = Emitter(cont)
        await em.run({"runId": run_id, "sessionId": "s-1"})
        await em.step(5, "Applying approved plan")
        await em.token("Created.")
        await em.done({"runId": run_id, "messageId": "m-1", "outcome": {"status": "approved"}})
        await cont.close()

        reader = RedisChannel(run_id)
        out = await _drain(reader, replay=cursor)
        names = [o["event"] for o in out]
        assert names == ["run", "step", "token", "done"], \
            f"the continuation consumer must see ONLY continuation frames, got {names}"
    finally:
        await _cleanup(run_id)


async def test_from_zero_the_old_eos_eats_the_continuation(live_redis):
    """Documents the exact live bug shape so the cursor can never be silently dropped:
    a from-zero consumer stops at the ORIGINAL turn's marker and the `done` is lost."""
    run_id = f"itest-p03-{uuid.uuid4()}"
    try:
        await _seed_original_turn(run_id)
        cont = RedisChannel(run_id)
        em = Emitter(cont)
        await em.done({"runId": run_id, "messageId": "m-1", "outcome": {}})
        await cont.close()

        out = await _drain(RedisChannel(run_id), replay="0")
        names = [o["event"] for o in out]
        assert "done" not in names, "from cursor 0 the old EOS stops the pump before done"
        assert names == ["run", "step", "interrupt"]
    finally:
        await _cleanup(run_id)


async def test_memory_channel_cursor_is_equivalent_and_fresh_is_zero():
    ch = RunChannel("r-1")
    assert await ch.current_cursor() == 0          # fresh channel — from-the-start is safe
    await ch.emit("step", {"index": 1, "label": "x"})
    await ch.emit("token", {"text": "y"})
    assert await ch.current_cursor() == 2          # newest already-published frame id
