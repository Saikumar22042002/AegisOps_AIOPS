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
import uuid
from datetime import UTC

import structlog
from sqlalchemy import select

from ..db.models import Resource, Run
from ..db.session import session_scope
from . import inventory
from .supervisor import RunSupervisor, get_supervisor, hb_key

log = structlog.get_logger(__name__)

# P0/D5: "applying" removed — a phantom status written by nothing at HEAD (verified:
# zero writers). Prompt 3: the durable engine is LIVE and writes the P3 machine's
# transient statuses (executing/verifying/scheduled) — a worker killed mid-workflow now
# parks a run in those states, so the stuck-run sweep must cover them too (found by the
# activation regression pass; without this a crashed durable run stayed stuck forever).
EXECUTING_STATES = ("running", "executing", "verifying", "scheduled")
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
            except Exception as exc:  # noqa: BLE001
                # Prod-hardening (2026-08-17): an UNREACHABLE heartbeat store is UNKNOWN,
                # not expired. Redriving on unknown is exactly wrong: the idempotency
                # claims that make a redrive safe live in the same Redis and are equally
                # unreachable — a blind redrive could run a concurrent apply. Skip this
                # pass; the run is retried next sweep once Redis answers.
                log.warning("reconciler.heartbeat_unknown_skipping", run_id=run_id,
                            error=str(exc)[:120])
                summary["skipped_heartbeat"] += 1
                continue
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
        summary.update(await self.sweep_tf_hygiene())
        try:
            from .retention import sweep_retention
            summary.update(await sweep_retention())   # PR-4: no-op unless retention is configured
        except Exception as e:  # noqa: BLE001 — retention must never break the reconcile pass
            log.warning("reconciler.retention_sweep_failed", error=str(e))
        return summary

    async def sweep_tf_hygiene(self, max_age_days: int = 7) -> dict[str, int]:
        """PR-1: (a) stray .tfplan files older than the threshold are removed (crashed runs
        whose terminal cleanup never fired); (b) a DESTROYED resource's per-state workspace
        whose terraform state holds zero resources for > threshold is pruned. Sweeper-only —
        no chat request can trigger a prune; every action is logged."""
        from datetime import datetime, timedelta

        from ..settings import get_settings
        from ..tools import terraform as tf_tools

        settings = get_settings()
        out = {"stray_plans_removed": 0, "state_workspaces_pruned": 0}
        try:
            # BINDING: non-terminal runs' plans are never stray — awaiting_approval may
            # legitimately wait days for a human.
            async with session_scope() as s:
                keep = {str(r.id) for r in (await s.execute(
                    select(Run).where(Run.status.in_(
                        ("running", "awaiting_approval"))))).scalars()}  # P0/D5: no "applying"
            out["stray_plans_removed"] = tf_tools.sweep_stray_plan_files(
                settings, max_age_days, keep_run_ids=keep)
        except Exception as e:  # noqa: BLE001 — hygiene must never break the reconcile pass
            log.warning("reconciler.stray_plan_sweep_failed", error=str(e))
        try:
            cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
            async with session_scope() as s:
                rows = (await s.execute(
                    select(Resource).where(Resource.status == "destroyed",
                                           Resource.state_workspace.is_not(None),
                                           Resource.updated_at < cutoff)
                )).scalars().all()
                for r in rows:
                    if tf_tools.prune_destroyed_state_workspace(settings, r.workspace,
                                                                r.state_workspace):
                        out["state_workspaces_pruned"] += 1
                        r.state_workspace = None    # pruned — never point at a removed dir
        except Exception as e:  # noqa: BLE001
            log.warning("reconciler.state_prune_failed", error=str(e))
        return out

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
        from .events import Emitter, create_channel
        from .runner import run_graph

        # The re-driven result must be PERSISTED like any API-driven run — run_graph only
        # executes the graph; status + assistant message land via _persist_result. Without this,
        # a successful redrive left the run `running` and the NEXT sweep force-failed it
        # (observed live at the Phase-2 gate: `resumed` → 60s later `marked_failed`, two actions
        # for one run — and a redriven APPLY would have had its applied outcome stamped over
        # with `failed`).
        async with session_scope() as s:
            run = await s.get(Run, uuid.UUID(run_id))
            org_id = str(run.org_id) if run else ""
            session_id = str(run.session_id) if run and run.session_id else ""

        channel = create_channel(run_id)

        async def _drive() -> None:
            from ..api.chat import _force_terminal, _persist_result
            emitter = Emitter(channel)
            try:
                await emitter.run({"runId": run_id, "sessionId": session_id or None})
                # Continue from the checkpoint (no new input). A1 idempotency makes the apply
                # safe to re-enter — it will return the stored result or abort, never double-apply.
                res = await run_graph(run_id, channel)
                state = res["state"]
                error = res.get("error")
                status_ = "failed" if error else ("awaiting_approval" if res["interrupted"] else "completed")
                if error and not state.get("answer"):
                    state = {**state,
                             "answer": f"⚠️ This run was recovered after an interruption and then "
                                       f"failed: {error}. Nothing was changed beyond what the Logs show.",
                             "outcome": state.get("outcome") or {"status": "failed", "error": error}}
                if session_id:
                    msg_id = await _persist_result(run_id, session_id, org_id, state, status_)
                    if not res["interrupted"]:
                        await emitter.done({"messageId": msg_id, "runId": run_id, "traceId": run_id,
                                            "outcome": state.get("outcome") or {"status": status_}})
                else:  # no session to persist an answer into — still reach a terminal state
                    await _force_terminal(run_id, "recovered after an interruption (no session)")
            except Exception as exc:  # noqa: BLE001 — B5: a failed recovery must still terminate
                log.error("reconciler.redrive_failed", run_id=run_id, error=str(exc))
                await _force_terminal(run_id, f"recovery re-drive failed: {exc}")
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
            from ..settings import get_settings

            while True:
                try:
                    summary = await self.sweep()
                    await self.sweep_orphans()  # D2: rebuild any invisible inventory orphan
                    # P0 ledger: replay any spilled usage records into Postgres (idempotent
                    # by record id — a replay can never double-count). Best-effort here;
                    # the journal survives until every record lands.
                    try:
                        from ..integrations.usage_ledger import replay_spill
                        await replay_spill(get_settings())
                    except Exception as e:  # noqa: BLE001 — never break the reconcile pass
                        log.warning("reconciler.ledger_replay_failed", error=str(e))
                    # PR-6: publish the operator-alert gauges from the sweep result.
                    from ..metrics import STRANDED_RUNS
                    STRANDED_RUNS.set((summary.get("resumed", 0)) + (summary.get("failed", 0)))
                    if get_settings().aegisops_drift == "on":
                        from . import drift  # D3: cloud drift/orphan reconciliation
                        d = await drift.sweep()
                        from ..metrics import DRIFT_FINDINGS
                        if isinstance(d, dict):
                            DRIFT_FINDINGS.labels(kind="drift").set(d.get("drift", 0))
                            DRIFT_FINDINGS.labels(kind="orphan").set(d.get("orphans", 0))
                except Exception as e:  # noqa: BLE001 — a sweep failure must not kill the loop
                    from ..metrics import RECONCILER_SWEEP_FAILURES
                    RECONCILER_SWEEP_FAILURES.inc()
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
