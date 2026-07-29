"""Shared fakes for the GW-1 gateway tests: a faked Telegram API, at two levels.

* `FakeTransport` implements the channel-agnostic `Transport` protocol and records every call.
  It can be told to rate-limit, to reject edits, or to fail a specific operation — which is how
  the streaming ladder's batching / 429 backoff / edit-failure fallback are tested without HTTP.
* `bot_api(routes)` builds an `httpx.MockTransport` that answers real Bot API response bodies,
  so `TelegramClient`'s error MAPPING (429 → RateLimited, "not modified" → EditNotModified,
  409 → Conflict) is tested against the shapes Telegram actually sends.

Nothing here touches the network, a datastore, or a real bot token.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.gateways.transport import Button, EditNotModified, RateLimited, TransportError


@dataclass
class SentMessage:
    chat_id: str
    text: str
    buttons: list[Button] | None = None


@dataclass
class EditCall:
    chat_id: str
    message_id: str
    text: str
    buttons: list[Button] | None = None
    at: float = 0.0


@dataclass
class FakeTransport:
    """A recording `Transport`. Every knob below models a real Telegram behaviour."""

    max_text_len: int = 4096

    sent: list[SentMessage] = field(default_factory=list)
    edits: list[EditCall] = field(default_factory=list)
    deleted: list[tuple[str, str]] = field(default_factory=list)
    callbacks: list[tuple[str, str, bool]] = field(default_factory=list)
    drafts: list[tuple[str, str]] = field(default_factory=list)

    #: Raise RateLimited(retry_after) on the Nth edit (1-based). 0 = never.
    rate_limit_edit_at: int = 0
    rate_limit_retry_after: float = 2.0
    #: Raise TransportError on every edit from the Nth onwards (1-based). 0 = never.
    fail_edit_from: int = 0
    #: Raise EditNotModified on the Nth edit (benign — must NOT trip the fallback).
    not_modified_at: int = 0
    #: Whether this chat "supports" native draft streaming.
    draft_support: bool = False
    #: Fail `delete` (a preview we cannot clean up must not break the turn).
    fail_delete: bool = False

    _next_id: int = 1
    _clock: float = 0.0

    # A monotonic clock the streaming layer can be pointed at, so throttling is asserted
    # deterministically instead of with real sleeps.
    def now(self) -> float:
        return self._clock

    def advance(self, seconds: float) -> None:
        self._clock += seconds

    async def send(self, chat_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> str | None:
        self.sent.append(SentMessage(str(chat_id), text, buttons))
        mid = str(self._next_id)
        self._next_id += 1
        return mid

    async def edit(self, chat_id: str, message_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> None:
        n = len(self.edits) + 1
        if self.not_modified_at and n == self.not_modified_at:
            raise EditNotModified("message is not modified")
        if self.rate_limit_edit_at and n == self.rate_limit_edit_at:
            raise RateLimited(self.rate_limit_retry_after, "Too Many Requests")
        if self.fail_edit_from and n >= self.fail_edit_from:
            raise TransportError("Bad Request: MESSAGE_ID_INVALID")
        self.edits.append(EditCall(str(chat_id), str(message_id), text, buttons, self._clock))

    async def delete(self, chat_id: str, message_id: str) -> None:
        if self.fail_delete:
            raise TransportError("Bad Request: message can't be deleted")
        self.deleted.append((str(chat_id), str(message_id)))

    async def answer_callback(self, callback_id: str, text: str = "", *,
                             alert: bool = False) -> None:
        self.callbacks.append((str(callback_id), text, alert))

    async def supports_drafts(self, chat_id: str) -> bool:
        return self.draft_support

    async def send_draft(self, chat_id: str, text: str) -> bool:
        if not self.draft_support:
            return False
        self.drafts.append((str(chat_id), text))
        return True

    # ── assertions helpers ───────────────────────────────────────────────────────────────────

    @property
    def last_text(self) -> str:
        return self.sent[-1].text if self.sent else ""

    @property
    def all_text(self) -> str:
        return "\n".join(m.text for m in self.sent)


def bot_api(routes: dict[str, Any]) -> httpx.MockTransport:
    """An httpx.MockTransport answering `{method_name: (status, body_dict)}` for /bot<token>/…

    A route value may also be a list, consumed one response per call, so a retry path (e.g. the
    parse-mode fallback resend) can be given a different second answer.
    """
    state: dict[str, list] = {}
    for name, value in routes.items():
        state[name] = list(value) if isinstance(value, list) else [value]

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        queue = state.get(method)
        if not queue:
            return httpx.Response(404, json={"ok": False, "error_code": 404,
                                             "description": f"Not Found: {method}"})
        status, body = queue[0] if len(queue) == 1 else queue.pop(0)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def ok(result: Any) -> tuple[int, dict]:
    return 200, {"ok": True, "result": result}


def err(code: int, description: str, **params: Any) -> tuple[int, dict]:
    body: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if params:
        body["parameters"] = params
    return code if code in (400, 403, 404, 409, 429) else 200, body


def update(text: str, *, user_id: int = 4242, chat_id: int | None = None,
           username: str = "sai", update_id: int = 1) -> dict:
    """A minimal Telegram `message` update, as getUpdates would deliver it."""
    return {"update_id": update_id,
            "message": {"message_id": 100 + update_id,
                        "from": {"id": user_id, "username": username, "first_name": "Sai"},
                        "chat": {"id": chat_id if chat_id is not None else user_id, "type": "private"},
                        "text": text}}


def callback_update(token: str, *, user_id: int = 4242, chat_id: int | None = None,
                    callback_id: str = "cb1", message_id: int = 999,
                    update_id: int = 1) -> dict:
    """A minimal Telegram `callback_query` update (an inline-button press)."""
    return {"update_id": update_id,
            "callback_query": {
                "id": callback_id,
                "from": {"id": user_id, "username": "sai", "first_name": "Sai"},
                "data": token,
                "message": {"message_id": message_id,
                            "chat": {"id": chat_id if chat_id is not None else user_id}}}}


def json_body(request: httpx.Request) -> dict:
    return json.loads(request.content or b"{}")
