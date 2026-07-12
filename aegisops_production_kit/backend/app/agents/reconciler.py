"""Stranded-run reconciler (B3).

A periodic sweep that brings runs to a terminal state after a worker crash. A run in a
non-terminal *executing* state (`running`/`applying`) whose heartbeat has expired and which no
worker is actively driving is **stranded** — the process that owned it died. For each stranded
run the reconciler either:

* **resumes** it from the LangGraph checkpoint when the graph has a next step
  (`aget_state().next` non-empty) — safe to re-drive because the idempotency wait-or-abort (A1)
  guarantees the apply cannot run twice; or
* **marks it failed honestly** ("recovered after an interruption — nothing was changed beyond
  what the Logs show") when there is nothing to resume.

`awaiting_approval` runs are deliberately left untouched — they are waiting on a human, not
stranded — so the reconciler only ever queries the executing states.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select

from ..db.models import Run
from ..db.session import session_scope
from . import inventory
from .supervisor import RunSupervisor, get_supervisor, hb_key

log = structlog.get_logger(__name__)

EXECUTING_STATES = ("running", "applying")
SWEEP_INTERVAL = 60  # seconds between sweeps


class Reconciler:
    def __init__(self, supervisor: RunSupervisor | None = None) -> None:
        self.supervisor = supervisor or get_supervisor()
        self._task: asyncio.Task | None = None

    async def sweep(self) -> dict[str, int]:
        """One reconciliation pass. Returns a summary (for observability + tests)."""
        from ..cache.redis import get_redis

        redis = get_redis()
        summary = {"resumed": 0, "failed": 0, "skipped_live": 0, "skipped_heartbeat": 0}
        async with session_scope() as s:
            candidates = [str(r.id) for r in (await s.execute(
                select(Run).where(Run.status.in_(EXECUTING_STATES))
            )).scalars()]

        for run_id in candidates:
            if self.supervisor.is_live(run_id):
                summary["skipped_live"] += 1  # this worker is driving it right now
                continue
            try:
                heartbeat_alive = bool(await redis.exists(hb_key(run_id)))
            except Exception:  # noqa: BLE001 — treat an unreachable heartbeat as expired
                heartbeat_alive = False
            if heartbeat_alive:
                summary["skipped_heartbeat"] += 1  # another worker is driving it
                continue

            # Stranded: no live drive here, heartbeat expired → recover it.
            if await self._is_resumable(run_id):
                await self._redrive(run_id)
                summary["resumed"] += 1
                log.info("reconciler.resumed", run_id=run_id)
            else:
                await self._mark_failed(run_id)
                summary["failed"] += 1
                log.info("reconciler.marked_failed", run_id=run_id)
        return summary

    async def sweep_orphans(self) -> dict[str, int]:
        """D2 orphan sweep: recover 'invisible orphans' — runs that applied real infrastructure
        but whose inventory row is missing (an interrupted write). The row is rebuilt from the
        recovery payload the run's outcome carries — no cloud read. Bounded + idempotent: a run
        whose row already exists (the normal case) is a cheap no-op. All recoveries commit in one
        transaction."""
        summary = {"checked": 0, "recovered": 0}
        async with session_scope() as s:
            runs = (await s.execute(
                select(Run).where(Run.outcome["status"].astext == "applied")
                .order_by(Run.created_at.desc()).limit(500)
            )).scalars().all()
            for run in runs:
                summary["checked"] += 1
                try:
                    if await inventory.recover_missing(s, run):
                        summary["recovered"] += 1
                        log.info("reconciler.orphan_recovered", run_id=str(run.id))
                except Exception as e:  # noqa: BLE001 — one bad run must not stop the sweep
                    log.warning("reconciler.orphan_recover_failed", run_id=str(run.id), error=str(e))
        if summary["recovered"]:
            log.info("reconciler.orphans_recovered", **summary)
        return summary

    async def _is_resumable(self, run_id: str) -> bool:
        try:
            from .graph import get_graph
            snap = await get_graph().aget_state({"configurable": {"thread_id": run_id}})
            return bool(getattr(snap, "next", None))
        except Exception as e:  # noqa: BLE001 — no/unreadable checkpoint ⇒ not resumable
            log.warning("reconciler.state_read_failed", run_id=run_id, error=str(e))
            return False

    async def _redrive(self, run_id: str) -> None:
        from .events import create_channel
        from .runner import run_graph

        channel = create_channel(run_id)

        async def _drive() -> None:
            try:
                # Continue from the checkpoint (no new input). A1 idempotency makes the apply
                # safe to re-enter — it will return the stored result or abort, never double-apply.
                await run_graph(run_id, channel)
            finally:
                await channel.close()

        self.supervisor.run(run_id, _drive)

    async def _mark_failed(self, run_id: str) -> None:
        from ..api.chat import _force_terminal
        await _force_terminal(
            run_id, "recovered after an interruption — nothing was changed beyond what the Logs show"
        )

    async def start(self, interval: float = SWEEP_INTERVAL) -> None:
        if self._task and not self._task.done():
            return  # idempotent: never accumulate sweep loops if start() is called twice

        async def _loop() -> None:
            while True:
                try:
                    await self.sweep()
                    await self.sweep_orphans()  # D2: rebuild any invisible inventory orphan
                except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
                    log.error("reconciler.sweep_failed", error=str(e))
                await asyncio.sleep(interval)

        self._task = asyncio.create_task(_loop())
        log.info("reconciler.started", interval=interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()


_reconciler: Reconciler | None = None


def get_reconciler() -> Reconciler:
    global _reconciler
    if _reconciler is None:
        _reconciler = Reconciler()
    return _reconciler
