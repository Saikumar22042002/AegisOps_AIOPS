"""GW-1 (unit tier) — the Telegram adapter and the channel-agnostic render/driver layers.

No datastores, no network: the Bot API is faked at the httpx layer (`gw_fakes.bot_api`) so the
error MAPPING is asserted against the response bodies Telegram really sends, and the driver is
exercised with a recording `FakeTransport`.

What these lock down:

* an UNLINKED sender gets the how-to-link reply and nothing else — no run, no data, no
  acknowledgement that any account exists;
* a linked but READ-ONLY user cannot initiate a run (RBAC via the bound user);
* every outbound message is redacted, High-confidentiality answers are withheld behind a deep
  link, and long output is truncated with a link rather than clipped;
* `source="telegram"` reaches `prepare_run`, so channel provenance is recorded on the run;
* 429 → RateLimited(retry_after from Telegram), "not modified" → the BENIGN EditNotModified,
  409 → Conflict; and the parse-mode fallback resends as plain text instead of losing a message;
* draft-capability is learned from real use and cached, never required.
"""

from __future__ import annotations

import pytest

from app.gateways import driver, identity, render
from app.gateways.telegram import api as tg_api
from app.gateways.telegram import poller as tg_poller
from app.gateways.transport import EditNotModified, RateLimited, TransportError
from app.schemas.auth import User as AuthUser
from app.settings import Settings
from tests.gw_fakes import FakeTransport, bot_api, err, ok, update

TOKEN = "test-token-not-a-real-secret"


def _settings(**over) -> Settings:
    base = {"aegisops_telegram": "on", "telegram_bot_token": TOKEN,
            "web_public_url": "http://localhost:3000"}
    base.update(over)
    return Settings(**base)


def _bound(roles: list[str], *, session_id: str | None = None) -> identity.BoundIdentity:
    return identity.BoundIdentity(
        identity_id="11111111-1111-1111-1111-111111111111",
        org_id="22222222-2222-2222-2222-222222222222",
        user_id="33333333-3333-3333-3333-333333333333",
        channel="telegram", channel_user_id="4242", channel_chat_id="4242",
        username="sai", email="sai@example.com", name="Sai", roles=roles,
        active_session_id=session_id)


# ── command parsing ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,cmd,rest", [
    ("/link ABCD-EFGH", "/link", "ABCD-EFGH"),
    ("/link@aegisops_bot ABCD-EFGH", "/link", "ABCD-EFGH"),   # group-chat suffix stripped
    ("/NEW", "/new", ""),
    ("  /help  ", "/help", ""),
    ("provision an s3 bucket", "", "provision an s3 bucket"),
    ("", "", ""),
])
def test_parse_command(text, cmd, rest):
    assert driver.parse_command(text) == (cmd, rest)


# ── the unlinked sender gets exactly one thing ───────────────────────────────────────────────


async def test_unlinked_sender_gets_only_how_to_link(monkeypatch):
    monkeypatch.setattr(identity, "resolve", _async_none)
    called: list[str] = []

    async def _never(**kwargs):
        called.append("prepare_run")
        raise AssertionError("an unlinked sender must never reach prepare_run")

    monkeypatch.setattr("app.api.chat.prepare_run", _never, raising=False)
    t = FakeTransport()
    await driver.handle_inbound(
        driver.Inbound(channel="telegram", channel_user_id="9", chat_id="9",
                       text="destroy the production vpc"),
        t, _settings())

    assert len(t.sent) == 1
    body = t.last_text
    assert "/link" in body and "Generate code" in body
    assert not called
    # No org, no username, no confirmation that any account exists.
    assert "sai" not in body.lower() and "northwind" not in body.lower()


async def test_unlinked_sender_may_only_use_link(monkeypatch):
    """Even an explicit command from an unbound sender is refused — /link is the only door."""
    monkeypatch.setattr(identity, "resolve", _async_none)
    t = FakeTransport()
    for text in ("/new", "/status", "/unlink", "/help"):
        await driver.handle_inbound(
            driver.Inbound(channel="telegram", channel_user_id="9", chat_id="9", text=text),
            t, _settings())
    assert len(t.sent) == 4
    assert all("Generate code" in m.text for m in t.sent)


# ── RBAC follows the bound user ──────────────────────────────────────────────────────────────


async def test_readonly_bound_user_cannot_initiate(monkeypatch):
    """S3 on the web says read-only roles cannot initiate; the gateway says the same words."""
    monkeypatch.setattr(identity, "resolve", _returns(_bound(["auditor"])))

    async def _never(**kwargs):
        raise AssertionError("a read-only role must never reach prepare_run")

    monkeypatch.setattr("app.api.chat.prepare_run", _never, raising=False)
    t = FakeTransport()
    await driver.handle_inbound(
        driver.Inbound(channel="telegram", channel_user_id="4242", chat_id="4242",
                       text="provision an s3 bucket"),
        t, _settings())
    assert "Read-only roles cannot initiate workflows." in t.last_text


def test_bound_identity_auth_user_maps_capabilities():
    approver = _bound(["cloud-architect"]).auth_user()
    assert approver.can_approve and approver.can_initiate and approver.can_execute
    assert approver.display_roles == ["Cloud Architect"]
    assert approver.org_id and approver.user_id      # S0 scope is carried

    reader = _bound(["auditor"]).auth_user()
    assert not reader.can_initiate and not reader.can_approve

    # An unknown/legacy role string grants nothing.
    assert not _bound(["not-a-real-role"]).auth_user().can_initiate


# ── source tagging + the shared driver ───────────────────────────────────────────────────────


async def test_turn_uses_shared_driver_and_tags_source(monkeypatch):
    """The gateway must call the SAME prepare_run/build_drive the web route calls, with
    source='telegram'."""
    seen: dict = {}

    class _Prepared:
        run_id = "aaaaaaaa-0000-0000-0000-000000000000"
        session_id = "bbbbbbbb-0000-0000-0000-000000000000"
        org_id = "22222222-2222-2222-2222-222222222222"
        resolved_model = "gemini-3.5-flash"
        env = "Production"
        source = "telegram"
        initiator_user_id = "33333333-3333-3333-3333-333333333333"
        initial: dict = {}

    async def _prepare(**kwargs):
        seen.update(kwargs)
        return _Prepared()

    def _build_drive(prepared, channel):
        async def _drive():
            return None
        return _drive

    class _Supervisor:
        def run(self, run_id, drive):
            seen["supervisor_run"] = run_id

    async def _consume(run_id, channel, *, replay_after=0, stream=None):
        return driver.RunOutcome(run_id=run_id, session_id=_Prepared.session_id,
                                 answer="Bucket planned.", confidentiality="Low")

    monkeypatch.setattr(identity, "resolve", _returns(_bound(["devops-engineer"])))
    monkeypatch.setattr(identity, "set_active_session", _async_ok)
    monkeypatch.setattr("app.api.chat.prepare_run", _prepare, raising=False)
    monkeypatch.setattr("app.api.chat.build_drive", _build_drive, raising=False)
    monkeypatch.setattr("app.agents.events.create_channel", lambda run_id: object())
    monkeypatch.setattr("app.agents.supervisor.get_supervisor", lambda: _Supervisor())
    monkeypatch.setattr(driver, "_consume", _consume)

    t = FakeTransport()
    await driver.handle_inbound(
        driver.Inbound(channel="telegram", channel_user_id="4242", chat_id="4242",
                       text="provision an s3 bucket"),
        t, _settings())

    assert seen["source"] == "telegram"            # channel provenance reaches the run row
    assert seen["message"] == "provision an s3 bucket"
    assert seen["model"] is None                   # a chat turn never overrides the model
    assert seen["context"].cloud is None           # U4: never silently default the cloud
    assert seen["supervisor_run"] == _Prepared.run_id
    # The answer lands as the final edit of the streaming preview, not as a second message.
    assert "Bucket planned." in t.delivered
    assert len(t.sent) == 1


async def test_new_command_clears_the_conversation(monkeypatch):
    cleared: list = []

    async def _set(identity_id, session_id):
        cleared.append((identity_id, session_id))

    monkeypatch.setattr(identity, "resolve", _returns(_bound(["sre"], session_id="s-1")))
    monkeypatch.setattr(identity, "set_active_session", _set)
    t = FakeTransport()
    await driver.handle_inbound(
        driver.Inbound(channel="telegram", channel_user_id="4242", chat_id="4242", text="/new"),
        t, _settings())
    assert cleared == [("11111111-1111-1111-1111-111111111111", None)]
    assert "fresh conversation" in t.last_text


async def test_inbound_failure_is_reported_not_raised(monkeypatch):
    """A turn that blows up must say so in the chat, never escape into the poll loop."""

    async def _boom(*a, **k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(identity, "resolve", _boom)
    t = FakeTransport()
    await driver.handle_inbound(
        driver.Inbound(channel="telegram", channel_user_id="4242", chat_id="4242", text="hi"),
        t, _settings())
    assert "Something went wrong" in t.last_text and "RuntimeError" in t.last_text


# ── render: redaction, withholding, truncation ───────────────────────────────────────────────


def test_outbound_redacts_secrets():
    text = "created key AKIAIOSFODNN7EXAMPLE and password=hunter2 for you"
    out = render.outbound(text, limit=4096)
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "hunter2" not in out
    assert "REDACTED" in out


def test_outbound_withholds_high_confidentiality_behind_a_deep_link():
    out = render.outbound("here is the private key material", limit=4096, level="High",
                          deep_link="http://localhost:3000/?run=r1")
    assert "private key material" not in out
    assert "http://localhost:3000/?run=r1" in out
    assert "sensitive" in out.lower()


def test_outbound_truncates_with_a_link_never_silently():
    body = "word " * 4000
    out = render.outbound(body, limit=500, deep_link="http://localhost:3000/?run=r1")
    assert len(out) <= 500
    assert "Truncated for chat" in out
    assert "http://localhost:3000/?run=r1" in out


def test_outbound_short_text_is_untouched():
    assert render.outbound("all good", limit=4096) == "all good"


def test_how_to_link_names_the_exact_ui_location():
    out = render.how_to_link(_settings())
    assert "Settings" in out and "Connected accounts" in out
    assert "Generate code" in out
    assert "/link" in out


def test_approval_card_carries_shape_not_contents():
    payload = {"kind": "approval", "workflow": "aws.s3", "mode": "apply",
               "plan": {"summary": {"add": 3, "change": 1, "destroy": 0},
                        "diff": [{"address": "aws_s3_bucket.secret_name"}]},
               "policyChecks": [{"name": "Encryption at rest", "passed": False}]}
    card = render.approval_card(payload, settings=_settings(), run_id="r1", initiator="dev")
    assert "+3 ~1 -0" in card
    assert "aws.s3" in card and "apply" in card
    assert "1 policy check(s) failing" in card
    assert "aws_s3_bucket.secret_name" not in card      # the diff stays in the web UI
    assert "http://localhost:3000/?run=r1&tab=terraform" in card


def test_approval_card_flags_a_deviation():
    card = render.approval_card({"reason": "deviation", "plan": {}}, settings=_settings(),
                                run_id="r1")
    assert "deviation" in card.lower()


# ── Bot API error mapping (faked httpx) ──────────────────────────────────────────────────────


async def test_send_maps_429_to_rate_limited_with_telegram_retry_after():
    client = tg_api.TelegramClient(
        TOKEN, transport=bot_api({"sendMessage": err(429, "Too Many Requests: retry after 7",
                                                     retry_after=7)}))
    with pytest.raises(RateLimited) as exc:
        await client.send("1", "hi")
    assert exc.value.retry_after == 7.0
    await client.close()


async def test_edit_not_modified_is_benign_and_distinct():
    client = tg_api.TelegramClient(
        TOKEN, transport=bot_api({"editMessageText": err(400, "Bad Request: message is not modified")}))
    with pytest.raises(EditNotModified):
        await client.edit("1", "2", "same text")
    await client.close()


async def test_get_updates_maps_409_to_conflict():
    client = tg_api.TelegramClient(
        TOKEN, transport=bot_api({"getUpdates": err(409, "Conflict: terminated by other getUpdates request")}))
    with pytest.raises(tg_api.TelegramConflict):
        await client.get_updates(0, timeout_s=1)
    await client.close()


async def test_other_errors_are_transport_errors():
    client = tg_api.TelegramClient(
        TOKEN, transport=bot_api({"sendMessage": err(403, "Forbidden: bot was blocked by the user")}))
    with pytest.raises(TransportError):
        await client.send("1", "hi")
    await client.close()


async def test_send_falls_back_to_plain_text_when_markdown_is_rejected():
    """Agent answers contain underscores and asterisks in resource names. A parse failure must
    resend without parse_mode, never drop the message."""
    client = tg_api.TelegramClient(TOKEN, transport=bot_api({"sendMessage": [
        err(400, "Bad Request: can't parse entities: unexpected end of entity"),
        ok({"message_id": 55}),
    ]}))
    assert await client.send("1", "my_bucket_*name*") == "55"
    await client.close()


async def test_token_never_appears_in_an_error_message():
    client = tg_api.TelegramClient(
        TOKEN, transport=bot_api({"sendMessage": err(400, "Bad Request: chat not found")}))
    with pytest.raises(TransportError) as exc:
        await client.send("1", "hi")
    assert TOKEN not in str(exc.value)
    await client.close()


async def test_draft_capability_is_learned_and_cached():
    """sendMessageDraft is optional. One unavailable answer disables it for that chat forever;
    the caller is never forced to use it."""
    client = tg_api.TelegramClient(TOKEN, transport=bot_api({
        "sendMessageDraft": err(400, "Bad Request: method not found"),
    }))
    assert await client.supports_drafts("1") is True      # unknown ⇒ optimistic, no probe call
    assert await client.send_draft("1", "partial") is False
    assert await client.supports_drafts("1") is False     # learned, cached
    assert await client.send_draft("1", "more") is False  # short-circuits, no further call
    await client.close()


async def test_draft_success_marks_capability_supported():
    client = tg_api.TelegramClient(TOKEN, transport=bot_api({"sendMessageDraft": ok(True)}))
    assert await client.send_draft("1", "partial") is True
    assert await client.supports_drafts("1") is True
    await client.close()


async def test_drop_pending_updates_skips_a_backlog():
    """A bot messaged while the API was down must not replay hours of requests as live runs."""
    client = tg_api.TelegramClient(TOKEN, transport=bot_api({"getUpdates": [
        ok([{"update_id": 500, "message": {}}]),   # newest only (offset=-1)
        ok([]),                                     # the acknowledging call
    ]}))
    assert await client.drop_pending_updates() == 501
    await client.close()


# ── update mapping ───────────────────────────────────────────────────────────────────────────


def test_to_inbound_maps_a_message():
    inbound = tg_api.to_inbound(update("hello", user_id=7, chat_id=8, username="sai"))
    assert inbound is not None
    assert (inbound.channel, inbound.channel_user_id, inbound.chat_id) == ("telegram", "7", "8")
    assert inbound.text == "hello" and inbound.username == "sai"


def test_to_inbound_ignores_non_messages_and_edits():
    assert tg_api.to_inbound({"update_id": 1}) is None
    assert tg_api.to_inbound({"update_id": 1, "edited_message": {"text": "x"}}) is None
    assert tg_api.to_inbound({"update_id": 1, "message": {"text": "x"}}) is None  # no ids


def test_to_inbound_accepts_a_caption():
    u = update("", user_id=7)
    u["message"].pop("text")
    u["message"]["caption"] = "look at this"
    assert tg_api.to_inbound(u).text == "look at this"


# ── posture banner ───────────────────────────────────────────────────────────────────────────


def test_posture_says_linked_only_and_offers_no_allowlist():
    text = tg_poller.posture(_settings())
    assert "LINKED" in text
    assert "how-to-link" in text
    assert "four-eyes" in text
    # The waku pattern we deliberately did not adopt must not reappear in the posture text.
    assert "ALLOWED_USER" not in text and "allowlist" not in text.lower()


async def test_gateway_stays_off_when_flag_off():
    gw = tg_poller.TelegramGateway(_settings(aegisops_telegram="off"))
    assert await gw.start() is False


async def test_gateway_stays_off_without_a_token():
    gw = tg_poller.TelegramGateway(_settings(telegram_bot_token=""))
    assert await gw.start() is False


async def test_start_in_background_never_raises(monkeypatch):
    """waku's contract: a gateway problem must not break startup."""

    class _Boom(tg_poller.TelegramGateway):
        async def start(self):
            raise RuntimeError("telegram unreachable")

    monkeypatch.setattr(tg_poller, "TelegramGateway", _Boom)
    monkeypatch.setattr(tg_poller, "_gateway", None)
    assert await tg_poller.start_in_background(_settings()) is False


# ── small async helpers ──────────────────────────────────────────────────────────────────────


async def _async_none(*a, **k):
    return None


async def _async_ok(*a, **k):
    return None


def _returns(value):
    async def _fn(*a, **k):
        return value
    return _fn


def test_auth_user_shape_is_the_real_schema():
    """The gateway must hand RBAC guards the same model the web path uses."""
    assert isinstance(_bound(["org-admin"]).auth_user(), AuthUser)
