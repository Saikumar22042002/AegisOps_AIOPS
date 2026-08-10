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
from . import identity, notify, render
from .stream import PreviewStream
from .transport import Button, Transport, TransportError

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
class Callback:
    """One normalized inline-button press from any channel."""

    channel: str
    channel_user_id: str
    chat_id: str
    callback_id: str
    token: str
    message_id: str | None = None
    username: str | None = None


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
    # Stream the answer AS it is generated: one preview message, rewritten under a throttle,
    # with step/tool progress folded in above the text (see gateways/stream.py).
    stream = PreviewStream(transport, inbound.chat_id, settings)
    await stream.start()
    result = await _consume(prepared.run_id, channel, stream=stream)
    await _deliver(inbound, transport, settings, result, stream=stream)


async def _consume(run_id: str, channel, *, replay_after=0,
                   stream: PreviewStream | None = None) -> RunOutcome:
    """Drain the run's event frames — the same ones the browser receives — into a RunOutcome,
    pushing progress and tokens into `stream` AS they arrive.

    `replay_after` matters for an approval continuation: on the redis bus the run's stream still
    holds the ORIGINAL turn's frames, so a from-zero consumer would replay the plan and stop at
    that turn's terminal marker, never seeing the apply (STAB P0-3). The cursor comes from
    `resolve_approval_core`, captured before the drive started.
    """
    from ..api.chat import iter_events

    out = RunOutcome(run_id=run_id)
    tokens: list[str] = []
    async for frame in iter_events(channel, replay_after):
        event, data = frame.get("event"), frame.get("data") or {}
        if event == "run":
            out.session_id = data.get("sessionId") or out.session_id
        elif event == "step":
            label = data.get("label")
            if label:
                out.steps.append(str(label))
                if stream is not None:
                    await stream.progress(str(label))
        elif event == "token":
            text = data.get("text") or ""
            tokens.append(text)
            if stream is not None:
                await stream.token(text)
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


def final_body(result: RunOutcome, settings: Settings) -> tuple[str, list[Button] | None]:
    """The finished turn's text + any inline buttons (pure, so it is directly testable)."""
    body = result.answer.strip()
    if not body and result.error:
        body = f"⚠️ {result.error}"
    if not body and result.steps:
        # A run that only emitted progress (a plan that ended at the gate, an apply with no
        # narration) still says what it did rather than "no answer".
        body = "· " + "\n· ".join(result.steps[-3:])
    if not body:
        body = "(the run produced no answer — open it in AegisOps for the full timeline)"
    buttons = None
    if result.interrupt is not None:
        body = f"{body}\n\n{render.approval_card(result.interrupt, settings=settings, run_id=result.run_id or '')}"
        # The sender may or may not be allowed to approve — the buttons are offered either way
        # and the CALLBACK decides, because capability must be re-checked at click time, not at
        # render time (roles change, and a card can sit in a chat for days).
        buttons = notify.approval_buttons(result.run_id) if result.run_id else None
    return body, buttons


async def _deliver(inbound: Inbound, transport: Transport, settings: Settings,
                   result: RunOutcome, *, stream: PreviewStream | None = None) -> None:
    """Land the finished turn: the final edit in place, or one plain message without a preview."""
    deep = render.web_run_link(settings, result.run_id) if result.run_id else None
    body, buttons = final_body(result, settings)
    if stream is not None:
        await stream.finish(body, level=result.confidentiality, deep_link=deep, buttons=buttons)
        return
    text = render.outbound(body, limit=transport.max_text_len,
                           level=result.confidentiality, deep_link=deep)
    await transport.send(inbound.chat_id, text, buttons=buttons)


# ── approval callbacks (inline buttons) ──────────────────────────────────────────────────────


def parse_approval_token(token: str) -> tuple[str, str] | None:
    """`apv:<run_id>:<approved|rejected>` → (run_id, decision), or None if it isn't one.

    Purely structural. The token grants nothing: `handle_callback` re-resolves who pressed it
    and re-runs every server-side check.
    """
    parts = (token or "").split(":")
    if len(parts) != 3 or parts[0] != "apv":
        return None
    run_id, decision = parts[1].strip(), parts[2].strip()
    if not run_id or decision not in {"approved", "rejected"}:
        return None
    return run_id, decision


async def handle_callback(cb: Callback, transport: Transport, settings: Settings) -> None:
    """Handle one inline-button press. Never raises."""
    try:
        await _handle_callback(cb, transport, settings)
    except Exception as exc:  # noqa: BLE001 — the poller must survive any single bad press
        log.error("gateway.callback_failed", channel=cb.channel, error=str(exc))
        try:
            await transport.answer_callback(cb.callback_id,
                                            "Something went wrong — nothing was changed.",
                                            alert=True)
        except TransportError:
            pass


async def _handle_callback(cb: Callback, transport: Transport, settings: Settings) -> None:
    from ..api.chat import resolve_approval_core

    parsed = parse_approval_token(cb.token)
    if parsed is None:
        await transport.answer_callback(cb.callback_id, "Unrecognized action.", alert=True)
        return
    run_id, decision = parsed

    # Identity is re-resolved on every press: an Unlink (or a binding that never existed) means
    # this press has no platform identity at all, whatever the button says.
    bound = await identity.resolve(cb.channel, cb.channel_user_id)
    if bound is None:
        await transport.answer_callback(cb.callback_id, "This chat isn't linked to AegisOps.",
                                       alert=True)
        await transport.send(cb.chat_id, render.how_to_link(settings))
        return

    user = bound.auth_user()
    # RBAC at CLICK time. resolve_approval_core re-checks this too (and org scope,
    # awaiting-approval state and the in-flight lock) — answering here first just gives the
    # presser an immediate, specific reason instead of a generic failure.
    if not user.can_approve:
        await transport.answer_callback(
            cb.callback_id, "Approval requires Cloud Architect, Org Admin, or Platform Admin.",
            alert=True)
        return

    try:
        channel, cursor = await resolve_approval_core(
            run_id, decision=decision,
            rationale=f"via {cb.channel} by {bound.username}",
            user=user, settings=settings)
    except HTTPException as exc:
        # Every refusal the web endpoint would give, verbatim: cross-org (404),
        # already decided / not awaiting (409), a decision already in flight (409).
        detail = str(exc.detail)
        await transport.answer_callback(cb.callback_id, detail, alert=True)
        await transport.send(cb.chat_id, render.refusal(detail))
        return

    await transport.answer_callback(
        cb.callback_id, "Approved — applying." if decision == "approved" else "Rejected.")
    # Re-render the card without buttons so the decision can't be double-pressed from the chat.
    if cb.message_id:
        try:
            await transport.edit(cb.chat_id, cb.message_id,
                                 f"{'✅ Approved' if decision == 'approved' else '🚫 Rejected'}"
                                 f" by **{bound.username}** via {cb.channel}.\n"
                                 f"{render.web_run_link(settings, run_id)}")
        except TransportError:
            pass  # the card may be too old to edit — the decision already stands

    # The continuation (apply/destroy, or the halt on reject) streams back through the SAME
    # preview-edit path a chat turn uses — so "terraform apply running" appears live rather
    # than after a minute of silence.
    inbound = Inbound(channel=cb.channel, channel_user_id=cb.channel_user_id,
                      chat_id=cb.chat_id, text="", username=cb.username)
    stream = PreviewStream(transport, cb.chat_id, settings)
    await stream.start()
    result = await _consume(run_id, channel, replay_after=cursor, stream=stream)
    if not result.answer.strip() and not result.error and not result.steps:
        result.answer = ("Applied." if decision == "approved"
                         else "Rejected — nothing was changed.")
    await _deliver(inbound, transport, settings, result, stream=stream)
