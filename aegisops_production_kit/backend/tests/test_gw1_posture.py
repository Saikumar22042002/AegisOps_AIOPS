"""GW-1 (unit tier) — the gateway's reported posture, and the fact that no route reveals a token.

Two properties:

1. **The bot token never leaves the process.** It is an operator secret in `.env`; no endpoint
   returns it, and the status payload carries only whether one EXISTS. This test reads the real
   response shape and asserts the token value appears nowhere in it.
2. **The blocker is named accurately.** "Not enabled" has two causes needing different operator
   actions — the flag is off, or the flag is on with no token. Reporting a missing token as
   "the flag is off" sends the operator to fix the wrong file, so they are distinct `reason`s.
"""

from __future__ import annotations

import pytest

from app.api.gateways import _posture
from app.settings import Settings

SECRET = "1234567890:AAHfake-not-a-real-bot-token-value"


def _settings(**over) -> Settings:
    base = {"aegisops_telegram": "off", "telegram_bot_token": ""}
    base.update(over)
    return Settings(**base)


@pytest.mark.parametrize("flag,token,expected", [
    ("off", "",       (False, "flag_off")),
    ("off", SECRET,   (False, "flag_off")),   # a token alone must not enable anything
    ("on",  "",       (False, "no_token")),   # the exact posture of this install
    ("on",  SECRET,   (True,  None)),
])
def test_posture_names_the_real_blocker(flag, token, expected):
    assert _posture(_settings(aegisops_telegram=flag, telegram_bot_token=token)) == expected


def test_the_blocked_message_names_the_right_file_for_each_cause():
    """Third instance of one bug class, so it is pinned: the operator sentence must not blame
    the flag when the token is what is missing. Both callers read it from BLOCKED_MESSAGE."""
    from app.api.gateways import blocked_message

    flag_off = blocked_message(_settings(aegisops_telegram="off"))
    assert flag_off and "AEGISOPS_TELEGRAM=on" in flag_off

    no_token = blocked_message(_settings(aegisops_telegram="on", telegram_bot_token=""))
    assert no_token and "TELEGRAM_BOT_TOKEN is empty" in no_token
    assert "@BotFather" in no_token and ".env" in no_token
    assert "never shown or stored" in no_token
    # It must NOT tell the operator to turn on a flag that is already on.
    assert "set AEGISOPS_TELEGRAM=on" not in no_token

    assert blocked_message(_settings(aegisops_telegram="on", telegram_bot_token=SECRET)) is None


def test_a_missing_token_is_never_reported_as_flag_off():
    """The regression this test exists for: the panel used to say 'AEGISOPS_TELEGRAM=off' while
    the flag was ON and only the token was missing."""
    enabled, reason = _posture(_settings(aegisops_telegram="on", telegram_bot_token=""))
    assert enabled is False
    assert reason == "no_token" and reason != "flag_off"


async def test_the_status_route_returns_no_token(monkeypatch):
    """Whatever else the payload grows, the token value must never appear in it."""
    from app.api import gateways as gw

    async def _org_id(user):
        return "22222222-2222-2222-2222-222222222222"

    async def _status(org_id, user_id, *, channel):
        return {"channel": channel, "linked": False, "code_pending": False}

    monkeypatch.setattr(gw, "_org_id", _org_id)
    monkeypatch.setattr(gw.identity, "status", _status)

    class _User:
        user_id = "33333333-3333-3333-3333-333333333333"
        username = "sai"

    settings = _settings(aegisops_telegram="on", telegram_bot_token=SECRET)
    payload = await gw.telegram_status(user=_User(), settings=settings)

    assert payload["enabled"] is True
    assert SECRET not in str(payload)
    # Only derived facts are exposed — never the credential itself.
    assert set(payload) >= {"channel", "linked", "enabled", "reason", "bot_username"}
    assert not any(v == SECRET for v in payload.values())
    # And nothing that would claim per-worker reachability as if it were global (see the route).
    assert "listening" not in payload


async def test_the_link_code_route_returns_a_link_code_not_the_bot_token(monkeypatch):
    """The one secret the UI DOES show is the one-time LINK CODE — a per-user, single-use,
    expiring credential for binding a chat account. That is not the bot token, and the two must
    never be confused."""
    from app.api import gateways as gw

    async def _org_id(user):
        return "22222222-2222-2222-2222-222222222222"

    async def _generate(org_id, user_id, *, channel, issued_by, ttl_seconds):
        from datetime import datetime, timedelta, timezone
        return "ABCD-EFGH", datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    monkeypatch.setattr(gw, "_org_id", _org_id)
    monkeypatch.setattr(gw.identity, "generate_code", _generate)

    class _User:
        user_id = "33333333-3333-3333-3333-333333333333"
        username = "sai"

    settings = _settings(aegisops_telegram="on", telegram_bot_token=SECRET)
    payload = await gw.telegram_link_code(user=_User(), settings=settings)

    assert payload["code"] == "ABCD-EFGH"
    assert SECRET not in str(payload)          # the BOT token is still nowhere in sight
    assert "/link ABCD-EFGH" in payload["instructions"]
