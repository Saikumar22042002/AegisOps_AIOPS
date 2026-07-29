"""GW: progressive answer streaming over a chat channel, channel-agnostically.

A chat platform has no SSE. To make an answer *appear* as it is generated we post one preview
message and rewrite it — which is cheap for the reader and expensive for the API, because
platforms rate-limit edits hard. So the ladder:

    (a) DEFAULT — preview + edit:
        · accumulate tokens, and edit at most once per `gateway_edit_min_interval_ms`
          AND only once at least `gateway_edit_min_chars` new characters have arrived, so a
          fast token stream produces a readable trickle instead of an edit storm;
        · a `▌` cursor while generating, removed by the final edit;
        · step/tool progress lines fold into the SAME preview, above the answer, so the reader
          sees "planning…" / "terraform apply running" without a second message;
        · on 429 we back off by exactly the delay the channel asked for (never a guess), and
          raise the floor interval for the rest of the turn;
        · on a real edit failure we degrade ONCE: stop editing, send one plain final message,
          and clean up the abandoned preview. "Message is not modified" is NOT a failure — it
          means our text is unchanged, so treating it as one would tear down a healthy preview.

    (b) IF AVAILABLE — native drafts (Telegram's `sendMessageDraft`): smoother than edits and
        not edit-rate-limited, used for the in-progress pushes when the chat supports it. The
        capability is detected from real use and never required: the moment a draft push says
        "unsupported", the edit ladder takes over for the rest of the turn.

The final message is always a real message (a draft is not a delivered answer), composed through
`render.outbound` so it is redacted, withheld if High-confidentiality, and truncated with a web
deep link rather than clipped.

`clock` and `sleep` are injected so the throttling, the backoff and the fallback are asserted
deterministically in tests instead of with real waits (same seam idea as
`exec_loop._request_reapproval`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from ..settings import Settings
from . import render
from .transport import Button, EditNotModified, RateLimited, Transport, TransportError

log = structlog.get_logger(__name__)

#: How many progress lines stay visible above the streaming answer.
MAX_PROGRESS_LINES = 3

#: What the preview says before any token or step has arrived.
INITIAL_TEXT = "⏳ Working…"


class PreviewStream:
    """One streaming answer in one chat. Create → `start()` → `progress()`/`token()` → `finish()`."""

    def __init__(self, transport: Transport, chat_id: str, settings: Settings, *,
                 clock: Callable[[], float] | None = None,
                 sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        self.transport = transport
        self.chat_id = str(chat_id)
        self.settings = settings
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep

        self._interval = max(0.0, settings.gateway_edit_min_interval_ms / 1000.0)
        self._min_chars = max(1, settings.gateway_edit_min_chars)

        self.message_id: str | None = None
        self._answer: list[str] = []
        self._progress: list[str] = []
        self._rendered = ""            # what the channel currently displays
        # What was displayed, measured the two ways the batching gate needs: how much ANSWER
        # text had been shown, and which progress lines. Comparing whole-text LENGTHS instead
        # would stall the stream whenever the text changed without changing length — exactly
        # what a rotating progress list does ("· step 1/2/3" → "· step 2/3/4").
        self._rendered_answer_len = 0
        self._rendered_progress: tuple[str, ...] = ()
        self._last_edit_at = 0.0
        self._backoff_until = 0.0
        self._degraded = False         # an edit really failed → stop editing, fall back at finish
        self._drafts: bool | None = None   # None = not yet asked
        self.edits = 0                 # observable for tests/ops
        self.draft_pushes = 0

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Post the preview message. A failure here degrades to "no preview" — the turn still
        delivers its answer as a plain message at the end."""
        try:
            self.message_id = await self.transport.send(self.chat_id, INITIAL_TEXT)
            self._mark_rendered(INITIAL_TEXT)
        except TransportError as exc:
            log.warning("gateway.preview_start_failed", error=str(exc))
            self._degraded = True

    async def progress(self, label: str) -> None:
        """A step/tool line, shown above the answer while generating."""
        label = (label or "").strip()
        if not label or (self._progress and self._progress[-1] == label):
            return
        self._progress.append(label)
        del self._progress[:-MAX_PROGRESS_LINES]
        await self._maybe_push()

    async def token(self, text: str) -> None:
        """Answer text as it arrives."""
        if not text:
            return
        self._answer.append(text)
        await self._maybe_push()

    async def finish(self, text: str, *, level: str | None = None,
                     deep_link: str | None = None,
                     buttons: list[Button] | None = None) -> None:
        """Deliver the final answer: one last edit in place, or one plain message if degraded."""
        final = render.outbound(text, limit=self.transport.max_text_len, level=level,
                               deep_link=deep_link)
        if self._degraded or self.message_id is None:
            await self._fallback(final, buttons)
            return
        # Sleep out any 429 backoff — the final message must actually land.
        remaining = self._backoff_until - self._clock()
        if remaining > 0:
            await self._sleep(remaining)
        try:
            await self._edit(final, buttons=buttons)
        except EditNotModified:
            pass  # already showing exactly this — delivered
        except RateLimited as exc:
            await self._sleep(exc.retry_after)
            try:
                await self._edit(final, buttons=buttons)
            except TransportError:
                await self._fallback(final, buttons)
        except TransportError:
            await self._fallback(final, buttons)

    # ── internals ────────────────────────────────────────────────────────────────────────────

    def _compose(self, *, generating: bool) -> str:
        """Progress lines above, answer below, `▌` while generating."""
        parts: list[str] = []
        if self._progress:
            parts.append("\n".join(f"· {p}" for p in self._progress))
        body = "".join(self._answer)
        if body:
            parts.append(body + (render.STREAM_CURSOR if generating else ""))
        elif generating:
            parts.append(render.STREAM_CURSOR)
        text = "\n\n".join(parts) or INITIAL_TEXT
        # An in-progress preview is bounded the same way the final message is; the tail is what
        # matters while streaming, so keep the END of an over-long body.
        limit = self.transport.max_text_len
        return text if len(text) <= limit else "…" + text[-(limit - 1):]

    def _answer_len(self) -> int:
        return sum(len(chunk) for chunk in self._answer)

    def _due(self) -> bool:
        """Is a push warranted right now?

        Two gates, both required: the interval must have elapsed, and there must be something
        materially new — either `min_chars` of fresh ANSWER text (that is the batching rule for
        a token stream), or a changed progress list. Progress changes are rare and meaningful, so
        they are never starved by the character gate.
        """
        now = self._clock()
        if now < self._backoff_until:
            return False
        if now - self._last_edit_at < self._interval:
            return False
        if tuple(self._progress) != self._rendered_progress:
            return True
        return self._answer_len() - self._rendered_answer_len >= self._min_chars

    async def _maybe_push(self) -> None:
        if self._degraded or self.message_id is None or not self._due():
            return
        text = self._compose(generating=True)
        # Prefer a native draft when the chat supports one: smoother, and not edit-rate-limited.
        if self._drafts is None:
            try:
                self._drafts = await self.transport.supports_drafts(self.chat_id)
            except Exception:  # noqa: BLE001 — capability probing must never break a turn
                self._drafts = False
        if self._drafts:
            try:
                if await self.transport.send_draft(self.chat_id, text):
                    self.draft_pushes += 1
                    self._mark_rendered(text)
                    return
                self._drafts = False   # unsupported after all → edit ladder for the rest
            except RateLimited as exc:
                self._backoff_until = self._clock() + exc.retry_after
                return
            except TransportError:
                self._drafts = False
        try:
            await self._edit(text)
        except EditNotModified:
            self._last_edit_at = self._clock()   # benign: nothing changed, not a failure
        except RateLimited as exc:
            # Back off by exactly what the channel asked for, and raise the floor so the rest of
            # this turn is gentler.
            self._backoff_until = self._clock() + exc.retry_after
            self._interval = max(self._interval, exc.retry_after)
            log.info("gateway.stream_rate_limited", retry_after=exc.retry_after)
        except TransportError as exc:
            # A real edit failure: stop editing for this turn and fall back at finish().
            log.warning("gateway.stream_edit_failed", error=str(exc))
            self._degraded = True

    def _mark_rendered(self, text: str) -> None:
        """Record exactly what the channel now displays, in the terms `_due` compares."""
        self._rendered = text
        self._rendered_answer_len = self._answer_len()
        self._rendered_progress = tuple(self._progress)
        self._last_edit_at = self._clock()

    async def _edit(self, text: str, *, buttons: list[Button] | None = None) -> None:
        await self.transport.edit(self.chat_id, str(self.message_id), text, buttons=buttons)
        self._mark_rendered(text)
        self.edits += 1

    async def _fallback(self, final: str, buttons: list[Button] | None) -> None:
        """Send ONE normal message and clean up the abandoned preview."""
        try:
            await self.transport.send(self.chat_id, final, buttons=buttons)
        except TransportError as exc:
            log.error("gateway.stream_fallback_failed", error=str(exc))
            return
        if self.message_id is not None:
            try:
                await self.transport.delete(self.chat_id, str(self.message_id))
            except TransportError:
                pass  # an undeletable preview is cosmetic; the answer was delivered
            self.message_id = None
