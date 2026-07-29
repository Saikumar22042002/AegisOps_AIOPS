"""Telegram Bot API client implementing the channel-agnostic `Transport`.

Only httpx — no python-telegram-bot. Long-polling `getUpdates` means no public URL and no
webhook: the API calls Telegram, never the reverse.

Error mapping is the interesting part, because the layers above depend on it:

* HTTP/API 429 → `RateLimited(retry_after)` from Telegram's own `parameters.retry_after`, so the
  streaming layer backs off by the amount Telegram asked for rather than guessing.
* "message is not modified" → `EditNotModified`, which is BENIGN. Telegram rejects an edit whose
  text is byte-identical; treating that as a failure would tear down a healthy preview.
* everything else → `TransportError`, which the caller degrades on (fallback to a plain message).

`sendMessageDraft` is capability-probed per chat and cached: a `Bad Request`/unknown-method
answer marks the chat unsupported forever, and the edit ladder carries on. It is never required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import structlog

from ..transport import Button, EditNotModified, RateLimited, TransportError

log = structlog.get_logger(__name__)

#: Telegram's hard per-message text limit.
MAX_TEXT_LEN = 4096

#: Telegram error text for a no-op edit — benign, never a failure.
_NOT_MODIFIED = "message is not modified"

#: Substrings that mean "this method does not exist / is not available here". Used only to
#: mark the optional draft capability unsupported.
_METHOD_UNAVAILABLE = ("method not found", "bot_method_invalid", "method is not available",
                       "not enough rights", "unknown method")


class TelegramConflict(TransportError):
    """Another getUpdates consumer is already polling this bot token (409)."""


class TelegramClient:
    """A Transport over the Telegram Bot API."""

    max_text_len = MAX_TEXT_LEN

    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org",
                 timeout: float = 20.0,
                 transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None) -> None:
        self._token = token
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._timeout = timeout
        # `transport` is httpx's own injection seam (the same one used for proxies/retries).
        # Passing an httpx.MockTransport lets the error-mapping and fallback behaviour below be
        # tested against real Bot API response bodies with no network — same idea as
        # `exec_loop._request_reapproval`.
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # Per-chat draft capability, learned from real use (see supports_drafts).
        self._drafts: dict[str, bool] = {}

    # ── plumbing ─────────────────────────────────────────────────────────────────────────────

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            finally:
                self._client = None

    async def _call(self, method: str, payload: dict | None = None, *,
                    timeout: float | None = None) -> Any:
        """One Bot API call. Raises the mapped transport errors; returns the `result` field.

        The bot token lives in the URL path, so it is never included in an exception message or
        a log field — only the method name is.
        """
        try:
            resp = await self._http().post(f"{self._base}/{method}", json=payload or {},
                                           timeout=timeout or self._timeout)
        except httpx.HTTPError as exc:
            raise TransportError(f"{method}: transport error ({type(exc).__name__})") from exc
        try:
            body = resp.json()
        except ValueError:
            raise TransportError(f"{method}: non-JSON response (HTTP {resp.status_code})") from None
        if body.get("ok"):
            return body.get("result")

        description = str(body.get("description") or "")
        code = int(body.get("error_code") or resp.status_code or 0)
        low = description.lower()
        if code == 429:
            retry_after = float((body.get("parameters") or {}).get("retry_after") or 1.0)
            raise RateLimited(retry_after, f"{method}: {description}")
        if _NOT_MODIFIED in low:
            raise EditNotModified(f"{method}: {description}")
        if code == 409:
            raise TelegramConflict(f"{method}: {description}")
        raise TransportError(f"{method}: {description or f'HTTP {code}'}")

    # ── Transport surface ────────────────────────────────────────────────────────────────────

    @staticmethod
    def _keyboard(buttons: list[Button] | None) -> dict | None:
        if not buttons:
            return None
        return {"inline_keyboard": [[{"text": b.label, "callback_data": b.token} for b in buttons]]}

    async def send(self, chat_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> str | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "parse_mode": "Markdown",
                                   "link_preview_options": {"is_disabled": True}}
        kb = self._keyboard(buttons)
        if kb:
            payload["reply_markup"] = kb
        try:
            result = await self._call("sendMessage", payload)
        except TransportError as exc:
            # Our text is Markdown-ish (agent answers contain backticks, asterisks and
            # underscores in resource names). If Telegram rejects the ENTITIES, resend as plain
            # text rather than losing the message — the content matters, the formatting doesn't.
            if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                raise
            payload.pop("parse_mode", None)
            result = await self._call("sendMessage", payload)
        return str((result or {}).get("message_id")) if isinstance(result, dict) else None

    async def edit(self, chat_id: str, message_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                   "parse_mode": "Markdown",
                                   "link_preview_options": {"is_disabled": True}}
        kb = self._keyboard(buttons)
        if kb:
            payload["reply_markup"] = kb
        try:
            await self._call("editMessageText", payload)
        except EditNotModified:
            raise
        except RateLimited:
            raise
        except TransportError as exc:
            if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                raise
            payload.pop("parse_mode", None)
            await self._call("editMessageText", payload)

    async def delete(self, chat_id: str, message_id: str) -> None:
        await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def answer_callback(self, callback_id: str, text: str = "", *,
                              alert: bool = False) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            # Telegram caps callback answers at 200 chars.
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        await self._call("answerCallbackQuery", payload)

    async def supports_drafts(self, chat_id: str) -> bool:
        """Best known answer with NO extra API call.

        Capability is learned from real use: unknown ⇒ optimistic True, so the first genuine
        draft push does the discovery; `send_draft` caches False the moment Telegram says the
        method is unavailable here, and every later call short-circuits. We never probe with a
        synthetic (e.g. empty) draft — that would be a wasted call that a supporting chat could
        legitimately reject, permanently mislabelling it.
        """
        return self._drafts.get(str(chat_id), True)

    async def send_draft(self, chat_id: str, text: str) -> bool:
        """Publish an in-progress draft. Returns False (never raises) when unsupported."""
        key = str(chat_id)
        if self._drafts.get(key) is False:
            return False
        try:
            await self._call("sendMessageDraft", {"chat_id": chat_id, "text": text})
        except RateLimited:
            raise
        except TransportError as exc:
            low = str(exc).lower()
            if any(marker in low for marker in _METHOD_UNAVAILABLE) or "400" in low:
                if self._drafts.get(key) is not False:
                    log.info("telegram.drafts_unsupported", chat_id=key)
                self._drafts[key] = False
                return False
            # A transient failure must not permanently disable the capability.
            return False
        self._drafts[key] = True
        return True

    # ── polling ──────────────────────────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        result = await self._call("getMe")
        return result if isinstance(result, dict) else {}

    async def get_updates(self, offset: int, *, timeout_s: int) -> list[dict]:
        """One long-poll. The HTTP timeout must exceed the long-poll wait or every poll aborts."""
        result = await self._call(
            "getUpdates",
            {"offset": offset, "timeout": timeout_s,
             "allowed_updates": ["message", "edited_message", "callback_query"]},
            timeout=timeout_s + 10)
        return list(result or [])

    async def drop_pending_updates(self) -> int:
        """Skip the backlog on startup and return the next offset.

        A bot that was messaged while the API was down should not replay hours of requests as
        live runs the moment it comes back. `getUpdates(offset=-1)` returns only the newest
        update, which we then acknowledge.
        """
        try:
            latest = await self._call("getUpdates", {"offset": -1, "timeout": 0}, timeout=15)
        except TransportError:
            return 0
        rows = list(latest or [])
        if not rows:
            return 0
        next_offset = int(rows[-1]["update_id"]) + 1
        try:  # acknowledge the backlog so it is never delivered again
            await self._call("getUpdates", {"offset": next_offset, "timeout": 0}, timeout=15)
        except TransportError:
            pass
        return next_offset


def to_inbound(update: dict, channel: str = "telegram"):
    """Map a Telegram update to the channel-agnostic `Inbound`, or None if it isn't a message.

    `edited_message` is deliberately ignored: re-running an edited request would execute the same
    intent twice, which for an infrastructure platform is the wrong default.
    """
    from ..driver import Inbound

    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text") or message.get("caption") or ""
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if not sender.get("id") or not chat.get("id"):
        return None
    return Inbound(
        channel=channel,
        channel_user_id=str(sender["id"]),
        chat_id=str(chat["id"]),
        text=text,
        username=sender.get("username") or sender.get("first_name"),
        message_id=str(message.get("message_id")) if message.get("message_id") else None,
    )


def to_callback(update: dict, channel: str = "telegram"):
    """Map a Telegram `callback_query` (inline-button press) to the channel-agnostic `Callback`.

    Returns None when the update isn't a button press or lacks the ids we need to answer it.
    """
    from ..driver import Callback

    query = update.get("callback_query")
    if not isinstance(query, dict):
        return None
    sender = query.get("from") or {}
    message = query.get("message") or {}
    chat = message.get("chat") or {}
    if not query.get("id") or not sender.get("id") or not chat.get("id"):
        return None
    return Callback(
        channel=channel,
        channel_user_id=str(sender["id"]),
        chat_id=str(chat["id"]),
        callback_id=str(query["id"]),
        token=str(query.get("data") or ""),
        message_id=str(message.get("message_id")) if message.get("message_id") else None,
        username=sender.get("username") or sender.get("first_name"),
    )


async def sleep_backoff(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> None:
    """Bounded exponential backoff for a failing poll loop."""
    await asyncio.sleep(min(cap, base * (2 ** max(0, attempt - 1))))
