"""GW-1: the Telegram adapter — Bot API client, update mapping, long-poll loop.

Adopted from waku's `waku/gateway/telegram.py`:

* **long-polling** (`getUpdates`), so there is no public URL, no webhook, and no inbound port;
* a **startup posture banner** that says out loud who can reach the bot;
* a **background task that never takes the API down** — every failure is caught and logged;
* **Conflict warned exactly once** (a second poller on the same token is a config mistake, not
  a per-request error worth a log line every two seconds);
* **source tagging** so a message's channel of origin is recorded on the run.

Deliberately NOT adopted: `TELEGRAM_ALLOWED_USER`. A chat-id allowlist is not an identity — see
`gateways/identity.py`.

One deviation from waku's implementation (not its behaviour): waku uses python-telegram-bot,
which needs its own event loop on a daemon thread (`waku/gateway/telegram.py:132-139`). AegisOps
is already async and already depends on httpx, so the Bot API is polled directly from a lifespan
task — same mechanic, no new dependency, and it shuts down with the app.
"""
