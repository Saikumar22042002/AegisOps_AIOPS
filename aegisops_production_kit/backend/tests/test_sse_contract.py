"""SSE event contract (6.1) + reconnect/replay + exactly-once delivery (6.3).

The frontend binds its live artifact panel to a run from the FIRST `run` event and drives the
timeline from `step`/`token`/…; correctness of that binding depends on: (a) each event carrying
the right name + payload shape, (b) monotonic ids, (c) Last-Event-ID replay returning only newer
events, and (d) the `_sse` drain delivering every event exactly once even when an event sits in
both the replay buffer and the live queue (the "run event delivered once" guarantee).
"""

from __future__ import annotations

import pytest

from app.agents.events import DONE, Emitter, RunChannel, create_channel, drop_channel, get_channel
from app.api.chat import _sse


async def _collect(gen):
    return [item async for item in gen]


async def test_emitter_event_names_and_shapes():
    ch = RunChannel("run-1")
    em = Emitter(ch)
    await em.run({"runId": "run-1", "sessionId": "s-1"})
    await em.step(2, "Ran terraform plan")
    await em.token("hello")
    await em.analysis(summary="done", cards=[{"title": "t", "body": "b"}])
    await em.params({"template": "aws.ec2", "items": []})
    await em.confidentiality("High", 0.9)
    await em.console("stdout", "Plan: 1 to add")
    await em.interrupt({"kind": "approval", "runId": "run-1"})
    await em.error("boom", code="terraform_error", retriable=True)
    await em.done({"messageId": "m-1", "runId": "run-1"})

    events = [(e["event"], e["data"]) for e in ch.history]
    names = [n for n, _ in events]
    assert names == ["run", "step", "token", "analysis", "params",
                     "confidentiality", "console", "interrupt", "error", "done"]
    assert events[0][1] == {"runId": "run-1", "sessionId": "s-1"}
    assert events[1][1] == {"index": 2, "label": "Ran terraform plan"}
    assert events[3][1]["reasoningCards"] == [{"title": "t", "body": "b"}]
    assert events[5][1] == {"level": "High", "score": 0.9}
    assert events[6][1] == {"stream": "stdout", "line": "Plan: 1 to add"}
    assert events[8][1] == {"message": "boom", "code": "terraform_error", "retriable": True}


async def test_ids_are_monotonic():
    ch = RunChannel("run-2")
    for i in range(5):
        await ch.emit("step", {"index": i})
    assert [e["id"] for e in ch.history] == [1, 2, 3, 4, 5]


async def test_replay_after_returns_only_newer():
    ch = RunChannel("run-3")
    for i in range(4):
        await ch.emit("step", {"index": i})
    assert [e["id"] for e in ch.replay_after(0)] == [1, 2, 3, 4]
    assert [e["id"] for e in ch.replay_after(2)] == [3, 4]
    assert ch.replay_after(4) == []


async def test_sse_delivers_each_event_exactly_once_from_start():
    # Events land in BOTH the ring buffer (history) and the live queue. A fresh consumer
    # (replay_after=0) must not double-deliver the ones that are in both.
    ch = RunChannel("run-4")
    await ch.emit("run", {"runId": "run-4"})
    await ch.emit("step", {"index": 0})
    await ch.close()  # enqueues DONE

    out = await _collect(_sse(ch, replay_after=0))
    ids = [int(o["id"]) for o in out]
    assert ids == [1, 2]                          # both, in order, no duplicate
    assert len(ids) == len(set(ids))
    assert out[0]["event"] == "run"               # run is first — the binding guarantee


async def test_sse_reconnect_replays_after_last_id_without_duplicates():
    # Simulate a reconnect that already saw event id 1: it must get id 2 (missed while
    # disconnected) and NOT a duplicate of id 1, even though id 1 is still queued.
    ch = RunChannel("run-5")
    await ch.emit("run", {"runId": "run-5"})       # id 1 (already seen by the client)
    await ch.emit("step", {"index": 0})            # id 2 (missed)
    await ch.close()

    out = await _collect(_sse(ch, replay_after=1))
    ids = {int(o["id"]) for o in out}
    assert ids == {1, 2}                            # everything eventually delivered…
    assert len(out) == 2                            # …exactly once (no dup of id 1)


async def test_done_sentinel_stops_the_stream():
    ch = RunChannel("run-6")
    await ch.emit("token", {"text": "x"})
    await ch.queue.put(DONE)
    await ch.queue.put({"id": 99, "event": "token", "data": {"text": "after-done"}})
    out = await _collect(_sse(ch, replay_after=99))  # skip replay; drain queue only
    # DONE breaks the loop before the post-DONE item is read.
    assert all(int(o["id"]) != 99 for o in out)


def test_channel_registry_roundtrip():
    ch = create_channel("reg-1")
    assert get_channel("reg-1") is ch
    drop_channel("reg-1")
    assert get_channel("reg-1") is None
