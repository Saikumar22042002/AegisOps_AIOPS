"""GW-1: the Telegram long-poll loop, run as a lifespan background task.

Contract, adopted from waku's `start_in_background` (`waku/gateway/telegram.py:87-144`):

* returns **False quietly** when the gateway is off or unconfigured — not an error;
* **never raises** into startup: every failure is caught and logged, so a Telegram problem can
  never take the API down;
* prints a **posture banner** saying who can reach the bot;
* logs a **Conflict once** — a second poller on the same token is one configuration mistake, not
  a log line every two seconds.

Each update is handled in its own task so one slow run (a `terraform plan` can take a minute)
never blocks the poll loop, and so a per-turn failure is isolated. Concurrency is bounded by a
semaphore — the platform already caps active runs per org/user (PR-2a), and this keeps the
gateway from queueing an unbounded number of in-flight turns in memory.
"""

from __future__ import annotations

import asyncio

import structlog

from ...settings import Settings
from ..transport import TransportError
from .api import TelegramClient, TelegramConflict, sleep_backoff, to_inbound

log = structlog.get_logger(__name__)

#: Simultaneous in-flight chat turns per process.
MAX_CONCURRENT_TURNS = 8


def posture(settings: Settings) -> str:
    """Who can reach this bot, said out loud at startup.

    waku prints this because a silent default is how an assistant ends up serving strangers
    (`waku/gateway/telegram.py:32-39`). Ours reads differently on purpose: there is no
    allowlist to forget to set, because an unlinked sender has no identity at all.
    """
    return ("  reachable by: LINKED AegisOps accounts only — an unlinked sender receives only "
            "the how-to-link reply and can neither read nor change anything.\n"
            "  identity: one-time code issued by a web-authenticated user (Settings → "
            "Connected accounts); RBAC, org scope and four-eyes follow the bound user.\n"
            f"  web origin for deep links: {settings.web_base_url}")


class TelegramGateway:
    """Owns the client, the poll loop and the in-flight turn tasks for one process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = TelegramClient(settings.telegram_bot_token,
                                     api_base=settings.telegram_api_base)
        self._task: asyncio.Task | None = None
        self._turns: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(MAX_CONCURRENT_TURNS)
        self._conflict_warned = False
        self._offset = 0
        self.bot_username: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────────────────────

    async def start(self) -> bool:
        """Begin polling. Returns False (quietly) when it should not run at all."""
        if self.settings.aegisops_telegram != "on":
            log.info("telegram.disabled", reason="AEGISOPS_TELEGRAM is not 'on'")
            return False
        if not self.settings.telegram_bot_token:
            log.warning("telegram.not_configured",
                        detail="AEGISOPS_TELEGRAM=on but TELEGRAM_BOT_TOKEN is empty — "
                               "the gateway stays off")
            return False
        try:
            me = await self.client.get_me()
            self.bot_username = me.get("username")
        except TransportError as exc:
            # A bad token or an unreachable Telegram must not block startup: log it and stay off.
            log.error("telegram.start_failed", error=str(exc))
            await self.client.close()
            return False
        # Skip whatever piled up while we were down — a backlog must not become live runs.
        try:
            self._offset = await self.client.drop_pending_updates()
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram.backlog_skip_failed", error=str(exc))
        log.info("telegram.listening", bot=self.bot_username, mode="long-poll")
        for line in posture(self.settings).splitlines():
            log.info("telegram.posture", detail=line.strip())
        self._task = asyncio.create_task(self._loop(), name="telegram-poll")
        return True

    async def stop(self) -> None:
        """Cancel the loop and any in-flight turns, then release the HTTP client."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for t in list(self._turns):
            if not t.done():
                t.cancel()
        self._turns.clear()
        await self.client.close()
        log.info("telegram.stopped")

    # ── the loop ─────────────────────────────────────────────────────────────────────────────

    async def _loop(self) -> None:
        failures = 0
        while True:
            try:
                updates = await self.client.get_updates(
                    self._offset, timeout_s=self.settings.telegram_poll_timeout_s)
                failures = 0
                for update in updates:
                    self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                    self._spawn(update)
            except asyncio.CancelledError:
                raise
            except TelegramConflict:
                # Say it ONCE, plainly, and never dump this every poll: another instance is
                # already polling this token (waku's on_poll_error lesson).
                if not self._conflict_warned:
                    self._conflict_warned = True
                    log.warning(
                        "telegram.conflict",
                        detail="another process is already polling this bot token — this "
                               "gateway stays idle. Stop the other instance (or unset "
                               "AEGISOPS_TELEGRAM there) and restart to serve here.")
                await asyncio.sleep(15)
            except Exception as exc:  # noqa: BLE001 — the loop outlives any single failure
                failures += 1
                log.warning("telegram.poll_failed", error=str(exc), attempt=failures)
                await sleep_backoff(failures)

    def _spawn(self, update: dict) -> None:
        task = asyncio.create_task(self._handle(update))
        self._turns.add(task)
        task.add_done_callback(self._turns.discard)

    async def _handle(self, update: dict) -> None:
        """Route one update. Wrapped so a single bad turn can never kill the poller."""
        from ..driver import handle_inbound

        try:
            inbound = to_inbound(update)
            if inbound is None:
                return
            async with self._sem:
                await handle_inbound(inbound, self.client, self.settings)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("telegram.handle_failed", error=str(exc))


_gateway: TelegramGateway | None = None


def current() -> TelegramGateway | None:
    """The live gateway, or None. Read-only peek — never builds one."""
    return _gateway


async def start_in_background(settings: Settings) -> bool:
    """Start the gateway from the app lifespan. Never raises; returns whether it is listening."""
    global _gateway
    if _gateway is not None:
        return True
    gw = TelegramGateway(settings)
    try:
        started = await gw.start()
    except Exception as exc:  # noqa: BLE001 — a gateway problem must not break startup
        log.error("telegram.start_crashed", error=str(exc))
        try:
            await gw.client.close()
        except Exception:  # noqa: BLE001
            pass
        return False
    if started:
        _gateway = gw
    return started


async def stop_background() -> None:
    global _gateway
    if _gateway is None:
        return
    gw, _gateway = _gateway, None
    try:
        await gw.stop()
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram.stop_failed", error=str(exc))
