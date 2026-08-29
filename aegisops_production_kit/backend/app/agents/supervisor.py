"""RunSupervisor — owns live run execution as tracked tasks with a Redis heartbeat (B2).

Replaces the fire-and-forget `asyncio.create_task(_drive())` in the API. Each run is a *tracked*
task plus a per-run heartbeat key (`run:<id>:hb`, short TTL, refreshed periodically). This gives
the system three things it lacked:

* **liveness** — `is_live(run_id)` answers "is this run executing in THIS worker right now?" for
  reconnect decisions;
* **crash detection** — if the worker dies, the heartbeat key expires, so the stranded-run
  reconciler (B3) can tell a genuinely-abandoned run from one that's merely paused for approval;
* **graceful drain** — on shutdown, in-flight runs are cancelled and persisted `failed` with a
  real message rather than silently dropped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog

log = structlog.get_logger(__name__)

HEARTBEAT_TTL = 45      # seconds the heartbeat key lives (expires if the worker dies)
HEARTBEAT_INTERVAL = 15  # refresh cadence (< TTL so a live run never lets its key lapse)


def hb_key(run_id: str) -> str:
    return f"run:{run_id}:hb"


def cancel_key(run_id: str) -> str:
    return f"run:{run_id}:cancel"


CANCEL_TTL = 3600  # the cancel flag outlives any single stage; cleared at terminal time


async def request_cancel(run_id: str, *, hard: bool = True) -> None:
    """PR-3: raise the cooperative cancel flag. Checked at safe boundaries (pre-approval
    drive, between DAG steps) — NEVER mid-apply.

    `hard=True` additionally cancels the live asyncio drive in THIS worker — safe ONLY for
    a pre-approval run (classification/planning; its drive handles CancelledError).
    Prod-hardening (2026-08-17): the approval CONTINUATION registers under the same run_id
    while the run status is still `awaiting_approval` — hard-cancelling that task killed a
    live terraform apply mid-mutation while the record claimed "cancelled, nothing was
    changed". Callers must pass hard=False unless the run is provably pre-approval."""
    from ..cache.redis import get_redis

    await get_redis().set(cancel_key(run_id), "1", ex=CANCEL_TTL)
    if hard:
        get_supervisor().signal_cancel(run_id)


async def is_cancelled(run_id: str) -> bool:
    from ..cache.redis import get_redis

    try:
        return bool(await get_redis().exists(cancel_key(run_id)))
    except Exception:  # noqa: BLE001 — an unreachable flag store never forces a cancel
        return False


async def clear_cancel(run_id: str) -> None:
    from ..cache.redis import get_redis

    try:
        await get_redis().delete(cancel_key(run_id))
    except Exception:  # noqa: BLE001
        pass


class RunSupervisor:
    def __init__(self) -> None:
        self._runs: dict[str, asyncio.Task] = {}
        self._heartbeats: dict[str, asyncio.Task] = {}

    def is_live(self, run_id: str) -> bool:
        t = self._runs.get(run_id)
        return t is not None and not t.done()

    def signal_cancel(self, run_id: str) -> None:
        """PR-3: cancel the live pre-approval drive in this worker (cooperative — the flag
        is the source of truth; this just stops the idle await promptly). A run executing
        terraform is NOT force-killed here — its cancel is honored at the next step
        boundary, never mid-apply."""
        t = self._runs.get(run_id)
        if t is not None and not t.done():
            t.cancel()

    def live_run_ids(self) -> list[str]:
        return [rid for rid, t in self._runs.items() if not t.done()]

    def run(self, run_id: str, drive: Callable[[], Awaitable[None]]) -> None:
        """Register + start a tracked drive (a 0-arg async callable) plus its heartbeat."""
        if self.is_live(run_id):
            log.warning("supervisor.already_live", run_id=run_id)
            return
        self._heartbeats[run_id] = asyncio.create_task(self._heartbeat(run_id))

        async def _wrapped() -> None:
            try:
                await drive()
            finally:
                await self._deregister(run_id)

        self._runs[run_id] = asyncio.create_task(_wrapped())

    async def _heartbeat(self, run_id: str) -> None:
        from ..cache.redis import get_redis

        try:
            while True:
                try:
                    await get_redis().set(hb_key(run_id), "1", ex=HEARTBEAT_TTL)
                except Exception as e:  # noqa: BLE001 — heartbeat is best-effort
                    log.debug("supervisor.heartbeat_failed", run_id=run_id, error=str(e))
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:  # normal on deregister
            pass

    async def _deregister(self, run_id: str) -> None:
        hb = self._heartbeats.pop(run_id, None)
        if hb and not hb.done():
            hb.cancel()
        self._runs.pop(run_id, None)
        try:
            from ..cache.redis import get_redis
            await get_redis().delete(hb_key(run_id))
        except Exception:  # noqa: BLE001
            pass

    async def drain(self) -> None:
        """Shutdown: cancel every live drive and persist it `failed` — never silently dropped."""
        run_ids = self.live_run_ids()
        if not run_ids:
            return
        log.info("supervisor.drain", count=len(run_ids))
        for rid in run_ids:
            t = self._runs.get(rid)
            if t and not t.done():
                t.cancel()
        for rid in run_ids:
            t = self._runs.get(rid)
            if t:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await self._mark_failed(rid)
            await self._deregister(rid)

    async def _mark_failed(self, run_id: str) -> None:
        # Lazy import avoids a load-time cycle (api.chat imports this module).
        from ..api.chat import _force_terminal
        await _force_terminal(run_id, "worker shut down while this run was in flight")


_supervisor: RunSupervisor | None = None


def get_supervisor() -> RunSupervisor:
    global _supervisor
    if _supervisor is None:
        _supervisor = RunSupervisor()
    return _supervisor
