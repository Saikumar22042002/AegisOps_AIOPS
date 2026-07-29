"""GW: the abstract outbound surface a channel adapter must implement.

Keeping this a Protocol (not a base class) is what makes the streaming and driver layers
channel-agnostic AND testable: the tests drive a `FakeTransport` that records calls and can be
told to raise `RateLimited` or `TransportError`, with no HTTP anywhere.

`Button` is intentionally minimal — a label plus an opaque callback token — because that is the
common denominator across chat platforms. The token is parsed by the adapter, never trusted:
every callback re-resolves identity and re-checks RBAC server-side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """The channel refused an operation and the caller must degrade (never crash a run)."""


class RateLimited(TransportError):
    """The channel asked us to slow down. `retry_after` is in seconds."""

    def __init__(self, retry_after: float, message: str = "rate limited") -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after))


class EditNotModified(TransportError):
    """A benign edit rejection: the text is byte-identical to what is already displayed.

    Not a failure — it must NOT trip the edit-failure fallback, or an idle stream would tear
    down its own preview message.
    """


@dataclass(frozen=True)
class Button:
    label: str
    token: str  # opaque; the adapter maps it to/from its own callback payload


@runtime_checkable
class Transport(Protocol):
    """Everything the channel-agnostic layers are allowed to assume about a channel."""

    #: Hard per-message text limit; `render.outbound` truncates to fit it.
    max_text_len: int

    async def send(self, chat_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> str | None:
        """Post a new message. Returns its channel message id (None if the channel has none)."""
        ...

    async def edit(self, chat_id: str, message_id: str, text: str, *,
                   buttons: list[Button] | None = None) -> None:
        """Replace an existing message's text in place.

        Raises `EditNotModified` when the content is unchanged, `RateLimited` when the channel
        asks us to back off, `TransportError` on anything else.
        """
        ...

    async def delete(self, chat_id: str, message_id: str) -> None:
        """Remove a message. Best-effort — used to clean up an abandoned preview."""
        ...

    async def answer_callback(self, callback_id: str, text: str = "", *,
                              alert: bool = False) -> None:
        """Acknowledge a button press (and optionally show the user a message)."""
        ...

    async def supports_drafts(self, chat_id: str) -> bool:
        """True when this chat supports native draft streaming (Telegram's sendMessageDraft).

        Capability-probed and cached per chat; a False answer must never degrade anything —
        the edit-based ladder is the default path.
        """
        ...

    async def send_draft(self, chat_id: str, text: str) -> bool:
        """Publish an in-progress draft. Returns False when unsupported (never raises for that)."""
        ...
