"""SSE event bus bridging async graph execution to the HTTP SSE stream.

Two interchangeable backends behind one `Emitter`/`_sse` contract (B1):

* **memory** (`RunChannel`) — an in-process `asyncio.Queue` per run + a bounded ring buffer for
  Last-Event-ID replay. Single-worker; the rollback default.
* **redis** (`RedisChannel`) — a Redis Stream `run:<id>:events` (XADD on emit, XREAD BLOCK on
  read). Any worker can publish; any worker (or a reconnecting client) can read, so streaming and
  approval-resume survive horizontal scale. The stream self-evicts on a terminal event (TTL).

Both channels expose the same surface the consumer (`api/chat.py:_sse`) uses — `replay_after()`
+ a `.queue` drained until the `DONE` sentinel — so the Emitter and the SSE frame contract
(event names, payload shapes, monotonic-ish ids) are byte-identical across backends.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any

from ..settings import get_settings

DONE = object()  # sentinel: stream finished

# Redis-mode tuning.
_STREAM_MAXLEN = 2000          # cap the stream length (approximate XADD trim)
_TERMINAL_TTL = 3600           # seconds a terminal stream lingers for late reconnects, then evicts
_EOS = "__eos__"               # in-stream terminal marker (never delivered to the client)
_XREAD_BLOCK_MS = 2000


class RunChannel:
    """In-process (memory-mode) channel. Behavior unchanged from the original bus."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.history: deque[dict[str, Any]] = deque(maxlen=1000)
        self.seq = 0
        self.finished = False

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        self.seq += 1
        payload = {"id": self.seq, "event": event, "data": data}
        self.history.append(payload)
        await self.queue.put(payload)

    def replay_after(self, last_id: int) -> list[dict[str, Any]]:
        try:
            after = int(last_id)
        except (TypeError, ValueError):
            after = 0
        return [e for e in self.history if e["id"] > after]

    async def close(self) -> None:
        self.finished = True
        await self.queue.put(DONE)


class RedisChannel:
    """Redis-Streams-backed channel. XADD on emit; a background XREAD pump feeds `.queue` so the
    existing `_sse` consumer works unchanged. Worker-agnostic: a channel for the same run_id on
    any worker reads the same stream."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.key = f"run:{run_id}:events"
        self.queue: asyncio.Queue = asyncio.Queue()
        self.finished = False
        self._pump: asyncio.Task | None = None

    def _redis(self):
        from ..cache.redis import get_redis
        return get_redis()

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        await self._redis().xadd(
            self.key, {"event": event, "data": json.dumps(data)},
            maxlen=_STREAM_MAXLEN, approximate=True,
        )

    def replay_after(self, last_id: Any) -> list[dict[str, Any]]:
        """Start the XREAD pump from `last_id` (a stream id, or 0/"" for the beginning) and return
        no synchronous replay — the pump delivers replay AND live through the queue uniformly, and
        `_sse` de-dups by id. Called once by `_sse` before it drains the queue."""
        cursor = "0" if not last_id or str(last_id) in ("0", "") else str(last_id)
        if self._pump is None:
            self._pump = asyncio.create_task(self._run_pump(cursor))
        return []

    async def _run_pump(self, cursor: str) -> None:
        r = self._redis()
        empty_polls = 0
        try:
            while True:
                resp = await r.xread({self.key: cursor}, block=_XREAD_BLOCK_MS, count=200)
                if not resp:
                    # No new entries this window. If the stream is gone (terminal + evicted) or we
                    # were closed, stop rather than block forever.
                    empty_polls += 1
                    if self.finished:
                        break
                    if empty_polls >= 2 and not await r.exists(self.key):
                        break
                    continue
                empty_polls = 0
                for _key, entries in resp:
                    for entry_id, fields in entries:
                        cursor = entry_id
                        ev = fields.get("event")
                        if ev == _EOS:
                            await self.queue.put(DONE)
                            return
                        try:
                            data = json.loads(fields.get("data") or "{}")
                        except json.JSONDecodeError:
                            data = {}
                        await self.queue.put({"id": entry_id, "event": ev, "data": data})
        except Exception:  # noqa: BLE001 — never wedge the SSE consumer; end the stream
            pass
        finally:
            await self.queue.put(DONE)

    async def close(self) -> None:
        """Publish the terminal marker (so cross-worker readers stop) and set the eviction TTL."""
        self.finished = True
        try:
            r = self._redis()
            await r.xadd(self.key, {"event": _EOS, "data": "{}"}, maxlen=_STREAM_MAXLEN, approximate=True)
            await r.expire(self.key, _TERMINAL_TTL)
        except Exception:  # noqa: BLE001
            await self.queue.put(DONE)

    async def exists(self) -> bool:
        return bool(await self._redis().exists(self.key))


# ── mode-aware factory ────────────────────────────────────────────────────────────────────────

_channels: dict[str, RunChannel] = {}  # memory-mode registry only


def _redis_mode() -> bool:
    return get_settings().aegisops_event_bus == "redis"


def create_channel(run_id: str):
    if _redis_mode():
        return RedisChannel(run_id)
    ch = RunChannel(run_id)
    _channels[run_id] = ch
    return ch


def get_channel(run_id: str):
    if _redis_mode():
        # Cross-worker: the stream lives in Redis, so any worker can serve it. Existence is checked
        # by the (async) caller via `channel.exists()`; here we just hand back a reader.
        return RedisChannel(run_id)
    return _channels.get(run_id)


def drop_channel(run_id: str) -> None:
    _channels.pop(run_id, None)


class Emitter:
    """Convenience wrapper passed to graph nodes via config['configurable']['emitter']."""

    def __init__(self, channel: RunChannel) -> None:
        self.ch = channel

    async def run(self, data: dict) -> None:
        """First event of a stream: binds the client's live panel to this run immediately."""
        await self.ch.emit("run", data)

    async def step(self, index: int, label: str) -> None:
        await self.ch.emit("step", {"index": index, "label": label})

    async def token(self, text: str) -> None:
        await self.ch.emit("token", {"text": text})

    async def analysis(self, summary: str, cards: list) -> None:
        await self.ch.emit("analysis", {"summary": summary, "reasoningCards": cards})

    async def params(self, data: dict) -> None:
        """Structured 'required inputs' request rendered as a param card in the message."""
        await self.ch.emit("params", data)

    async def reference(self, ref: dict) -> None:
        await self.ch.emit("reference", ref)

    async def confidentiality(self, level: str, score: float) -> None:
        await self.ch.emit("confidentiality", {"level": level, "score": score})

    async def console(self, stream: str, line: str) -> None:
        await self.ch.emit("console", {"stream": stream, "line": line})

    async def interrupt(self, data: dict) -> None:
        await self.ch.emit("interrupt", data)

    async def error(self, message: str, code: str = "error", retriable: bool = False) -> None:
        await self.ch.emit("error", {"message": message, "code": code, "retriable": retriable})

    async def done(self, data: dict) -> None:
        await self.ch.emit("done", data)
