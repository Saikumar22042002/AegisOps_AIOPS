"""B1 — Redis Streams event bus (worker-agnostic streaming).

Integration (live Redis). Ports the SSE contract to the Redis backend — the `Emitter` frame
shapes are identical, `_sse` de-dups by id, `done`/EOS stops the stream — and proves the headline
multi-worker property: publish from one channel (worker A), consume from a SEPARATE channel for
the same run (worker B). Each test uses a unique run id and trims its stream on exit.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents.events import DONE, Emitter, RedisChannel


async def _drain(channel, replay="0", limit=50):
    """Run the channel's pump (via replay_after) and collect queue items until DONE."""
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


async def test_redis_channel_roundtrip_frame_shapes(live_redis):
    run_id = f"itest-bus-{uuid.uuid4()}"
    ch = RedisChannel(run_id)
    em = Emitter(ch)
    try:
        await em.run({"runId": run_id, "sessionId": "s-1"})
        await em.step(2, "Ran terraform plan")
        await em.token("hello")
        await em.confidentiality("High", 0.9)
        await ch.close()  # EOS

        out = await _drain(ch)
        names = [o["event"] for o in out]
        assert names == ["run", "step", "token", "confidentiality"]
        assert out[0]["data"] == {"runId": run_id, "sessionId": "s-1"}
        assert out[1]["data"] == {"index": 2, "label": "Ran terraform plan"}
        assert out[3]["data"] == {"level": "High", "score": 0.9}
    finally:
        await _cleanup(run_id)


async def test_redis_bus_exactly_once_and_done_stops(live_redis):
    run_id = f"itest-bus-{uuid.uuid4()}"
    ch = RedisChannel(run_id)
    try:
        await ch.emit("token", {"text": "a"})
        await ch.emit("token", {"text": "b"})
        await ch.close()
        out = await _drain(ch)
        # Every event once, in order; EOS stops the stream (no marker leaks to the client).
        assert [o["data"]["text"] for o in out] == ["a", "b"]
        assert all(o["event"] != "__eos__" for o in out)
        ids = [o["id"] for o in out]
        assert len(ids) == len(set(ids))
    finally:
        await _cleanup(run_id)


async def test_redis_bus_replay_after_id(live_redis):
    run_id = f"itest-bus-{uuid.uuid4()}"
    producer = RedisChannel(run_id)
    try:
        await producer.emit("step", {"index": 0})
        await producer.emit("step", {"index": 1})
        await producer.emit("step", {"index": 2})
        await producer.close()

        first = await _drain(RedisChannel(run_id))
        assert [o["data"]["index"] for o in first] == [0, 1, 2]
        # Reconnect after the first event's id → only newer events replay (no duplicate).
        after = first[0]["id"]
        resumed = await _drain(RedisChannel(run_id), replay=after)
        assert [o["data"]["index"] for o in resumed] == [1, 2]
    finally:
        await _cleanup(run_id)


async def test_redis_bus_multi_worker_publish_A_consume_B(live_redis):
    """The headline B1 property: a run published on one channel instance (worker A) is fully
    readable from a DIFFERENT channel instance for the same run (worker B) — because the stream
    lives in Redis, not in either worker's memory."""
    run_id = f"itest-bus-{uuid.uuid4()}"
    worker_a = Emitter(RedisChannel(run_id))
    worker_b = RedisChannel(run_id)  # a separate consumer, as if on another process
    try:
        await worker_a.run({"runId": run_id, "sessionId": "s"})
        await worker_a.step(1, "plan")
        await worker_a.token("done")
        await worker_a.done({"messageId": "m", "runId": run_id})
        await worker_a.ch.close()

        out = await _drain(worker_b)
        assert [o["event"] for o in out] == ["run", "step", "token", "done"]
        assert out[-1]["data"]["messageId"] == "m"
    finally:
        await _cleanup(run_id)


async def test_redis_terminal_stream_sets_ttl(live_redis):
    run_id = f"itest-bus-{uuid.uuid4()}"
    ch = RedisChannel(run_id)
    try:
        await ch.emit("token", {"text": "x"})
        await ch.close()
        from app.cache.redis import get_redis
        ttl = await get_redis().ttl(f"run:{run_id}:events")
        assert ttl > 0, "a terminal stream must carry a TTL so it self-evicts"
    finally:
        await _cleanup(run_id)
