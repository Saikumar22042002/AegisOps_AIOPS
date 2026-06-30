"""SSE event bus bridging async graph execution to the HTTP SSE stream.

Each run has an asyncio.Queue; graph nodes push events (step/token/analysis/reference/
confidentiality/console/interrupt/done/error) and the /chat (or /chat/stream resume) endpoint
drains it. Queues are keyed by run_id so a run can be resumed on a new SSE connection after
the approval interrupt. Events are also kept in a bounded ring buffer to support Last-Event-ID
replay on reconnect.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

DONE = object()  # sentinel: stream finished


class RunChannel:
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
        return [e for e in self.history if e["id"] > last_id]

    async def close(self) -> None:
        self.finished = True
        await self.queue.put(DONE)


_channels: dict[str, RunChannel] = {}


def create_channel(run_id: str) -> RunChannel:
    ch = RunChannel(run_id)
    _channels[run_id] = ch
    return ch


def get_channel(run_id: str) -> RunChannel | None:
    return _channels.get(run_id)


def drop_channel(run_id: str) -> None:
    _channels.pop(run_id, None)


class Emitter:
    """Convenience wrapper passed to graph nodes via config['configurable']['emitter']."""

    def __init__(self, channel: RunChannel) -> None:
        self.ch = channel

    async def step(self, index: int, label: str) -> None:
        await self.ch.emit("step", {"index": index, "label": label})

    async def token(self, text: str) -> None:
        await self.ch.emit("token", {"text": text})

    async def analysis(self, summary: str, cards: list) -> None:
        await self.ch.emit("analysis", {"summary": summary, "reasoningCards": cards})

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
