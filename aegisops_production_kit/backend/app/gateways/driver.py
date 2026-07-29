"""GW: inbound message → the shared run driver. Channel-agnostic.

This module owns the gateway's control flow and nothing else:

    resolve identity → (unbound? how-to-link, stop) → command? handle it
      → RBAC: can this bound user initiate? → api.chat.prepare_run
      → api.chat.build_drive + supervisor.run  (the exact web path)
      → consume api.chat.iter_events (the exact frames the browser gets)
      → render one channel-safe reply

There is deliberately no branch here that reaches Terraform, the approved catalog, or the
approval interrupt: those live behind the graph, which this code only *starts*. A gateway turn
cannot be a wider capability than the same user's web turn, because it is literally the same
functions with a different renderer on the end.

Session policy: one chat = one AegisOps session per bound user, remembered on the identity row
(`active_session_id`). `/new` clears it, so the next message opens a fresh conversation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from fastapi import HTTPException

from ..settings import Settings
from . import identity, render
from .transport import Transport, TransportError

log = structlog.get_logger(__name__)


@dataclass
class Inbound:
    """One normalized inbound message from any channel."""

    channel: str
    channel_user_id: str
    chat_id: str
    text: str
    username: str | None = None
    message_id: str | None = None


@dataclass
class RunOutcome:
    """What a driven run produced, in channel-neutral terms."""

    run_id: str | None = None
    session_id: str | None = None
    answer: str = ""
    steps: list[str] = field(default_factory=list)
    interrupt: dict | None = None
    error: str | None = None
    confidentiality: str | None = None
    params: dict | None = None
    outcome: dict | None = None


# ── commands ─────────────────────────────────────────────────────────────────────────────────


def parse_command(text: str) -> tuple[str, str]:
    """`("/link", "ABCD-EFGH")` for a command, `("", text)` otherwise.

    Telegram appends `@botname` to commands in group chats; strip it so `/new@aegisbot` works.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return "", stripped
    head, _, rest = stripped.partition(" ")
    cmd = head.split("@", 1)[0].lower()
    return cmd, rest.strip()


async def handle_inbound(inbound: Inbound, transport: Transport, settings: Settings) -> None:
    """The gateway's entry point for one message. Never raises: a channel turn that fails must
    say so in the chat, not take down the poller."""
    try:
        await _handle(inbound, transport, settings)
    except Exception as exc:  # noqa: BLE001 — the poller must survive any single bad turn
        log.error("gateway.inbound_failed", channel=inbound.channel, error=str(exc))
        try:
            await transport.send(inbound.chat_id, render.refusal(
                f"Something went wrong handling that message: {type(exc).__name__}. "
                "Nothing was changed."))
        except TransportError:
            pass


async def _handle(inbound: Inbound, transport: Transport, settings: Settings) -> None:
    cmd, rest = parse_command(inbound.text)
    bound = await identity.resolve(inbound.channel, inbound.channel_user_id)

    # ── unbound: the ONLY thing that works is /link, and the only reply is how-to-link.
    if bound is None:
        if cmd == "/link" and rest:
            await _do_link(inbound, transport, settings, code=rest)
            return
        await transport.send(inbound.chat_id, render.how_to_link(settings))
        return

    if cmd == "/link":
        slug = await identity.org_slug(bound.org_id)
        await transport.send(inbound.chat_id,
                             f"Already linked to **{bound.username}**"
                             + (f" ({slug})" if slug else "")
                             + ". Send `/unlink` first to bind a different account.")
        return
    if cmd in ("/start", "/help"):
        await transport.send(inbound.chat_id, render.help_text())
        return
    if cmd == "/status":
        st = await identity.status(bound.org_id, bound.user_id, channel=inbound.channel)
        roles = ", ".join(bound.auth_user().display_roles) or "no roles"
        slug = await identity.org_slug(bound.org_id)
        await transport.send(
            inbound.chat_id,
            f"🔗 Linked to **{bound.username}**"
            + (f" · org **{slug}**" if slug else "")
            + f"\nRoles: {roles}\nLinked at: {st.get('linked_at') or '—'}\n"
            f"Conversation: {'active' if bound.active_session_id else 'new on next message'}")
        return
    if cmd == "/unlink":
        await identity.unlink(bound.org_id, bound.user_id, channel=inbound.channel,
                             actor=bound.username)
        await transport.send(inbound.chat_id,
                             "🔓 Unlinked. This chat no longer has access to AegisOps. "
                             "Generate a new code in Settings → Connected accounts to re-link.")
        return
    if cmd == "/new":
        await identity.set_active_session(bound.identity_id, None)
        await transport.send(inbound.chat_id,
                             "🆕 Started a fresh conversation. Send your request.")
        return
    if cmd:
        await transport.send(inbound.chat_id,
                             f"Unknown command `{cmd}`.\n\n{render.help_text()}")
        return
    if not rest:
        return  # nothing to run (a sticker, an empty edit) — silence is correct

    await _run_turn(inbound, transport, settings, bound, rest)


async def _do_link(inbound: Inbound, transport: Transport, settings: Settings,
                   *, code: str) -> None:
    try:
        bound = await identity.consume_code(
            channel=inbound.channel, code=code, channel_user_id=inbound.channel_user_id,
            channel_chat_id=inbound.chat_id, channel_username=inbound.username)
    except identity.LinkError as exc:
        await transport.send(inbound.chat_id, render.refusal(str(exc)))
        return
    except Exception as exc:  # noqa: BLE001 — never leak a store failure as "wrong code"
        log.error("gateway.link_failed", channel=inbound.channel, error=str(exc))
        await transport.send(inbound.chat_id, render.refusal(
            "Couldn't complete the link right now — the platform is unreachable. Try again."))
        return
    slug = await identity.org_slug(bound.org_id)
    await transport.send(inbound.chat_id, render.linked_greeting(bound.username, slug))


# ── running a turn through the shared driver ─────────────────────────────────────────────────


async def _run_turn(inbound: Inbound, transport: Transport, settings: Settings,
                    bound: identity.BoundIdentity, message: str) -> None:
    from ..api.chat import ChatContext, build_drive, prepare_run
    from ..agents.events import create_channel
    from ..agents.supervisor import get_supervisor

    user = bound.auth_user()
    # RBAC via the BOUND user, re-checked every turn (never cached in the chat). Same rule and
    # same wording as `require_initiator` on POST /chat.
    if not user.can_initiate:
        await transport.send(inbound.chat_id, render.refusal(
            "Read-only roles cannot initiate workflows."))
        return

    # Production is the platform default for a chat turn, exactly as the web composer defaults
    # (ChatContext). Cloud stays unset so `resolve_cloud` ASKS when it is ambiguous rather than
    # silently defaulting — U4.
    context = ChatContext(env="Production", cloud=None, region="us-east-1")
    try:
        prepared = await prepare_run(user=user, message=message, context=context,
                                     session_id=bound.active_session_id, model=None,
                                     settings=settings, source=inbound.channel)
    except HTTPException as exc:
        # A stale session pointer (deleted in the web UI) must not wedge the chat: drop it and
        # let the next message open a fresh conversation.
        if exc.status_code == 404 and bound.active_session_id:
            await identity.set_active_session(bound.identity_id, None)
            await transport.send(inbound.chat_id, render.refusal(
                "That conversation no longer exists — I've started a fresh one. Send it again."))
            return
        await transport.send(inbound.chat_id, render.refusal(str(exc.detail)))
        return

    if prepared.session_id != bound.active_session_id:
        await identity.set_active_session(bound.identity_id, prepared.session_id)

    channel = create_channel(prepared.run_id)
    get_supervisor().run(prepared.run_id, build_drive(prepared, channel))
    result = await _consume(prepared.run_id, channel)
    await _deliver(inbound, transport, settings, result)


async def _consume(run_id: str, channel) -> RunOutcome:
    """Drain the run's event frames — the same ones the browser receives — into a RunOutcome."""
    from ..api.chat import iter_events

    out = RunOutcome(run_id=run_id)
    tokens: list[str] = []
    async for frame in iter_events(channel, 0):
        event, data = frame.get("event"), frame.get("data") or {}
        if event == "run":
            out.session_id = data.get("sessionId") or out.session_id
        elif event == "step":
            label = data.get("label")
            if label:
                out.steps.append(str(label))
        elif event == "token":
            tokens.append(data.get("text") or "")
        elif event == "params":
            out.params = data
        elif event == "confidentiality":
            out.confidentiality = data.get("level")
        elif event == "interrupt":
            out.interrupt = data
        elif event == "error":
            out.error = data.get("message") or out.error
        elif event == "done":
            out.outcome = data.get("outcome")
    out.answer = "".join(tokens)
    return out


async def _deliver(inbound: Inbound, transport: Transport, settings: Settings,
                   result: RunOutcome) -> None:
    """Render the finished turn as one channel-safe message."""
    deep = render.web_run_link(settings, result.run_id) if result.run_id else None
    body = result.answer.strip()
    if not body and result.error:
        body = f"⚠️ {result.error}"
    if not body:
        body = "(the run produced no answer — open it in AegisOps for the full timeline)"
    if result.interrupt is not None:
        body = f"{body}\n\n{render.approval_card(result.interrupt, settings=settings, run_id=result.run_id or '')}"
    text = render.outbound(body, limit=transport.max_text_len,
                           level=result.confidentiality, deep_link=deep)
    await transport.send(inbound.chat_id, text)
