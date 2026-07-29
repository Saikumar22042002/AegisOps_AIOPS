"""GW-1 (unit tier) — inline approval buttons, the click-time re-check, and cross-channel push.

The load-bearing property: **a button press authorizes nothing.** The callback token is opaque
and forgeable-by-anyone-with-the-chat, so `handle_callback` re-resolves who pressed it and runs
the SAME server-side decision path the web endpoint runs (`resolve_approval_core`). These tests
prove each refusal reaches the presser, and that a stale or unlinked press changes nothing.

Covered here:

* token parsing rejects anything malformed (no run id smuggling, no third decision value);
* an UNLINKED presser is refused and gets only the how-to-link reply;
* a linked but NON-APPROVER presser is refused with the web's exact wording;
* **four-eyes is re-checked at click time** — the Production initiator's own press is refused
  even though the card was pushed to a chat they can read;
* a stale press (run already decided) surfaces the 409, not a silent no-op;
* an approved press disables the card so the decision cannot be double-pressed;
* the continuation is consumed from the cursor `resolve_approval_core` returned (STAB P0-3), so
  apply progress — not the replayed plan turn — is what streams back;
* the push list is four-eyes aware and survives one approver's chat being unreachable.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.gateways import driver, identity, notify, render
from app.gateways.telegram import api as tg_api
from app.gateways.transport import TransportError
from app.settings import Settings
from tests.gw_fakes import FakeTransport, callback_update

TOKEN = "test-token-not-a-real-secret"


def _settings(**over) -> Settings:
    base = {"aegisops_telegram": "on", "telegram_bot_token": TOKEN,
            "web_public_url": "http://localhost:3000"}
    base.update(over)
    return Settings(**base)


def _bound(roles: list[str], *, user_id: str = "33333333-3333-3333-3333-333333333333",
           username: str = "sai") -> identity.BoundIdentity:
    return identity.BoundIdentity(
        identity_id="11111111-1111-1111-1111-111111111111",
        org_id="22222222-2222-2222-2222-222222222222",
        user_id=user_id, channel="telegram", channel_user_id="4242", channel_chat_id="4242",
        username=username, email="sai@example.com", name="Sai", roles=roles,
        active_session_id=None)


def _cb(token: str = "apv:run-1:approved") -> driver.Callback:
    return driver.Callback(channel="telegram", channel_user_id="4242", chat_id="4242",
                           callback_id="cb1", token=token, message_id="999", username="sai")


def _returns(value):
    async def _fn(*a, **k):
        return value
    return _fn


# ── token parsing: structural only, grants nothing ───────────────────────────────────────────


@pytest.mark.parametrize("token,expected", [
    ("apv:abc-123:approved", ("abc-123", "approved")),
    ("apv:abc-123:rejected", ("abc-123", "rejected")),
    ("apv:abc-123:applied", None),        # not a decision the gate accepts
    ("apv::approved", None),              # no run id
    ("apv:abc-123", None),                # truncated
    ("xxx:abc-123:approved", None),       # wrong kind
    ("", None),
    ("apv:a:b:c:d", None),
])
def test_parse_approval_token(token, expected):
    assert driver.parse_approval_token(token) == expected


def test_approval_buttons_carry_both_decisions():
    buttons = notify.approval_buttons("run-9")
    assert [b.token for b in buttons] == ["apv:run-9:approved", "apv:run-9:rejected"]
    assert all(b.label for b in buttons)


# ── who may press ────────────────────────────────────────────────────────────────────────────


async def test_unlinked_press_is_refused_and_only_offers_linking(monkeypatch):
    monkeypatch.setattr(identity, "resolve", _returns(None))

    async def _never(*a, **k):
        raise AssertionError("an unlinked press must never reach resolve_approval_core")

    monkeypatch.setattr("app.api.chat.resolve_approval_core", _never, raising=False)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())

    assert t.callbacks and "isn't linked" in t.callbacks[-1][1]
    assert t.callbacks[-1][2] is True                     # shown as an alert, not a toast
    assert "Generate code" in t.last_text                 # the only thing they learn
    assert "run-1" not in t.last_text                     # not even the run id leaks


async def test_non_approver_press_is_refused_with_the_web_wording(monkeypatch):
    monkeypatch.setattr(identity, "resolve", _returns(_bound(["devops-engineer"])))

    async def _never(*a, **k):
        raise AssertionError("a non-approver must never reach resolve_approval_core")

    monkeypatch.setattr("app.api.chat.resolve_approval_core", _never, raising=False)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())
    assert "Cloud Architect, Org Admin, or Platform Admin" in t.callbacks[-1][1]


async def test_four_eyes_is_rechecked_at_click_time(monkeypatch):
    """A5 over a chat button: the Production initiator's own press is refused, with the same
    message the web endpoint gives — the card being in their chat authorizes nothing."""
    monkeypatch.setattr(identity, "resolve", _returns(_bound(["platform-admin"])))

    async def _four_eyes(*a, **k):
        raise HTTPException(403, "Four-eyes policy: you initiated this Production change — "
                                 "a different approver must review it.")

    monkeypatch.setattr("app.api.chat.resolve_approval_core", _four_eyes, raising=False)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())

    assert "Four-eyes" in t.callbacks[-1][1]
    assert "Four-eyes" in t.last_text
    assert not t.edits          # the card is NOT disabled — someone else still must decide


async def test_stale_press_surfaces_the_conflict(monkeypatch):
    """A card can sit in a chat for days. Pressing it after the fact must say so, not no-op."""
    monkeypatch.setattr(identity, "resolve", _returns(_bound(["org-admin"])))

    async def _conflict(*a, **k):
        raise HTTPException(409, "run is not awaiting approval")

    monkeypatch.setattr("app.api.chat.resolve_approval_core", _conflict, raising=False)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())
    assert "not awaiting approval" in t.callbacks[-1][1]
    assert "not awaiting approval" in t.last_text


async def test_cross_org_press_is_a_404_not_an_enumeration_hint(monkeypatch):
    monkeypatch.setattr(identity, "resolve", _returns(_bound(["org-admin"])))

    async def _not_found(*a, **k):
        raise HTTPException(404, "run not found")

    monkeypatch.setattr("app.api.chat.resolve_approval_core", _not_found, raising=False)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())
    assert "run not found" in t.callbacks[-1][1]


# ── the happy path ───────────────────────────────────────────────────────────────────────────


async def test_approved_press_drives_the_shared_path_and_disables_the_card(monkeypatch):
    seen: dict = {}

    async def _core(run_id, *, decision, rationale, user, settings):
        seen.update(run_id=run_id, decision=decision, rationale=rationale,
                    can_approve=user.can_approve, user_id=user.user_id)
        return ("CHANNEL", "1691234-0")     # (channel, continuation cursor)

    async def _consume(run_id, channel, *, replay_after=0, stream=None):
        seen["replay_after"] = replay_after
        seen["channel"] = channel
        seen["streamed"] = stream is not None
        return driver.RunOutcome(run_id=run_id, answer="✅ Applied: 3 resources created.",
                                 confidentiality="Low")

    monkeypatch.setattr(identity, "resolve", _returns(_bound(["cloud-architect"])))
    monkeypatch.setattr("app.api.chat.resolve_approval_core", _core, raising=False)
    monkeypatch.setattr(driver, "_consume", _consume)

    t = FakeTransport()
    await driver.handle_callback(_cb("apv:run-77:approved"), t, _settings())

    assert seen["run_id"] == "run-77" and seen["decision"] == "approved"
    assert seen["can_approve"] is True
    assert "telegram" in seen["rationale"] and "sai" in seen["rationale"]
    # STAB P0-3: the continuation is tailed from the cursor, never from zero.
    assert seen["replay_after"] == "1691234-0"
    # …and it streams through the same preview-edit path a chat turn uses.
    assert seen["streamed"] is True
    # The CARD (message 999) is rewritten without buttons, so the decision cannot be
    # double-pressed from the chat.
    card_edits = t.edits_to("999")
    assert card_edits and card_edits[-1].buttons is None
    assert "Approved" in card_edits[-1].text and "sai" in card_edits[-1].text
    # The continuation's result comes back to the chat, in the streaming preview.
    assert "Applied" in t.delivered


async def test_rejected_press_reports_nothing_changed(monkeypatch):
    async def _core(run_id, *, decision, rationale, user, settings):
        return ("CHANNEL", 0)

    monkeypatch.setattr(identity, "resolve", _returns(_bound(["org-admin"])))
    monkeypatch.setattr("app.api.chat.resolve_approval_core", _core, raising=False)
    monkeypatch.setattr(driver, "_consume",
                        lambda run_id, channel, replay_after=0, stream=None: _returns(
                            driver.RunOutcome(run_id=run_id, answer=""))())

    t = FakeTransport()
    await driver.handle_callback(_cb("apv:run-5:rejected"), t, _settings())
    assert "Rejected" in t.callbacks[-1][1]
    assert "nothing was changed" in t.delivered.lower()


async def test_an_uneditable_card_does_not_lose_the_decision(monkeypatch):
    """Telegram refuses edits to old messages. The decision already stands, so the turn must
    carry on and still deliver the continuation."""
    async def _core(run_id, *, decision, rationale, user, settings):
        return ("CHANNEL", 0)

    monkeypatch.setattr(identity, "resolve", _returns(_bound(["org-admin"])))
    monkeypatch.setattr("app.api.chat.resolve_approval_core", _core, raising=False)
    monkeypatch.setattr(driver, "_consume",
                        lambda run_id, channel, replay_after=0, stream=None: _returns(
                            driver.RunOutcome(run_id=run_id, answer="Applied."))())

    # Every edit fails: the card cannot be disabled AND the preview cannot be rewritten, so the
    # answer must arrive as a plain message. The decision itself already stands server-side.
    t = FakeTransport(fail_edit_from=1)
    await driver.handle_callback(_cb(), t, _settings())
    assert any("Applied." in m.text for m in t.sent)


async def test_malformed_token_is_answered_not_crashed():
    t = FakeTransport()
    await driver.handle_callback(_cb("garbage"), t, _settings())
    assert "Unrecognized" in t.callbacks[-1][1]


async def test_callback_failure_is_reported_not_raised(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("resolve exploded")

    monkeypatch.setattr(identity, "resolve", _boom)
    t = FakeTransport()
    await driver.handle_callback(_cb(), t, _settings())     # must not raise
    assert "Something went wrong" in t.callbacks[-1][1]


# ── the cross-channel push ───────────────────────────────────────────────────────────────────


async def test_push_is_four_eyes_aware(monkeypatch):
    """When four-eyes applies, the initiator is excluded — a card they cannot action is noise."""
    captured: dict = {}

    async def _targets(org_id, *, channel, exclude_user_id=None):
        captured["exclude"] = exclude_user_id
        return []

    monkeypatch.setattr(identity, "notifiable_approvers", _targets)
    monkeypatch.setattr(notify, "_transport_for", lambda channel, settings: FakeTransport())
    monkeypatch.setattr("app.gateways.notify.get_settings",
                        lambda: _settings(aegisops_four_eyes_for_production=True))

    await notify.approval_pending(run_id="r1", org_id="o1", env="Production",
                                  initiator_user_id="u-init", initiator_username="dev",
                                  interrupt_payload={})
    assert captured["exclude"] == "u-init"

    # Non-production: four-eyes does not apply, so the initiator may approve and is included.
    await notify.approval_pending(run_id="r1", org_id="o1", env="Staging",
                                  initiator_user_id="u-init", initiator_username="dev",
                                  interrupt_payload={})
    assert captured["exclude"] is None


async def test_push_survives_one_unreachable_approver(monkeypatch):
    """A blocked bot for one approver must not stop the others being notified."""
    t = FakeTransport()
    calls: list[str] = []

    async def _send(chat_id, text, *, buttons=None):
        calls.append(str(chat_id))
        if chat_id == "bad":
            raise TransportError("Forbidden: bot was blocked by the user")
        return "1"

    t.send = _send  # type: ignore[method-assign]
    targets = [_bound(["org-admin"], user_id="u1", username="a"),
               _bound(["org-admin"], user_id="u2", username="b"),
               _bound(["org-admin"], user_id="u3", username="c")]
    targets[1] = identity.BoundIdentity(**{**targets[1].__dict__, "channel_chat_id": "bad"})

    monkeypatch.setattr(identity, "notifiable_approvers", _returns(targets))
    monkeypatch.setattr(notify, "_transport_for", lambda channel, settings: t)
    monkeypatch.setattr("app.gateways.notify.get_settings", lambda: _settings())

    pushed = await notify.approval_pending(run_id="r1", org_id="o1", env="Staging",
                                           initiator_user_id=None, initiator_username="dev",
                                           interrupt_payload={"workflow": "aws.s3",
                                                              "mode": "apply"})
    assert pushed == 2 and len(calls) == 3


async def test_push_is_a_noop_when_no_gateway_is_running(monkeypatch):
    monkeypatch.setattr(notify, "_transport_for", lambda channel, settings: None)
    monkeypatch.setattr("app.gateways.notify.get_settings", lambda: _settings())
    assert await notify.approval_pending(run_id="r1", org_id="o1", env="Production",
                                         initiator_user_id=None, initiator_username=None,
                                         interrupt_payload={}) == 0


async def test_pushed_card_has_buttons_and_no_plan_contents(monkeypatch):
    t = FakeTransport()
    monkeypatch.setattr(identity, "notifiable_approvers",
                        _returns([_bound(["cloud-architect"])]))
    monkeypatch.setattr(notify, "_transport_for", lambda channel, settings: t)
    monkeypatch.setattr("app.gateways.notify.get_settings", lambda: _settings())

    payload = {"workflow": "aws.rds", "mode": "apply",
               "plan": {"summary": {"add": 2, "change": 0, "destroy": 1},
                        "diff": [{"address": "aws_db_instance.prod_secrets"}]}}
    assert await notify.approval_pending(run_id="r9", org_id="o1", env="Production",
                                         initiator_user_id="u-other",
                                         initiator_username="dev",
                                         interrupt_payload=payload) == 1
    msg = t.sent[-1]
    assert [b.token for b in (msg.buttons or [])] == ["apv:r9:approved", "apv:r9:rejected"]
    assert "+2 ~0 -1" in msg.text
    assert "aws_db_instance.prod_secrets" not in msg.text     # the diff stays in the web UI
    assert "http://localhost:3000/?run=r9" in msg.text


# ── update mapping ───────────────────────────────────────────────────────────────────────────


def test_to_callback_maps_a_button_press():
    cb = tg_api.to_callback(callback_update("apv:r1:approved", user_id=7, chat_id=8,
                                            callback_id="q1", message_id=55))
    assert cb is not None
    assert (cb.channel_user_id, cb.chat_id, cb.callback_id) == ("7", "8", "q1")
    assert cb.token == "apv:r1:approved" and cb.message_id == "55"


def test_to_callback_ignores_non_callbacks_and_incomplete_ones():
    assert tg_api.to_callback({"update_id": 1}) is None
    assert tg_api.to_callback({"update_id": 1, "message": {"text": "hi"}}) is None
    assert tg_api.to_callback({"update_id": 1, "callback_query": {"data": "x"}}) is None


def test_a_message_update_is_not_mistaken_for_a_callback():
    from tests.gw_fakes import update as msg_update
    assert tg_api.to_callback(msg_update("hello")) is None
    assert tg_api.to_inbound(callback_update("apv:r1:approved")) is None


def test_refusal_output_is_redacted():
    out = render.refusal("denied for token=abcdef123456789")
    assert "abcdef123456789" not in out and "REDACTED" in out
