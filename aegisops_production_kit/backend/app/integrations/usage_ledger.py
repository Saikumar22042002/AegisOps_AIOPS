"""Authoritative LLM usage/cost ledger (P0 — Redesign/06 §8.2, 07 item 0.3, defect D3).

Accounting truth lives HERE (PostgreSQL `llm_usage`); Langfuse remains observability.
Tokens are ground truth; `cost_usd` is a convenience snapshot derived from the prices
configured at write time (re-derivable from tokens at read time when prices change).

Durable-delivery semantics (an accounting record must never silently disappear):

    client-generated record UUID
      → fire-and-forget async INSERT with bounded retry (backoff + jitter)
      → on final failure: append to a local fsync'd spill journal
        (tokens/ids/labels only — never prompt or response content)
      → `aegisops_ledger_spill_total` metric + error log (loud, alertable)
      → the reconciler replays the journal into PostgreSQL idempotently
        (INSERT .. ON CONFLICT (id) DO NOTHING — replay cannot double-count).

The user-facing LLM response is never blocked: persistence is scheduled onto the
running event loop; only the spill append (fast, local) is synchronous. Contrast with
Langfuse (observability): a Langfuse outage degrades tracing silently; a ledger
persistence failure is loud and leaves a durable, replayable record.

P0 scope note: `purpose` carries coarse call-site labels (classify | generate |
answer_stream | embedding). Real purpose-based routing is P1; this module must not
grow routing logic.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_conf import get_logger
from ..settings import Settings

log = get_logger(__name__)

# Bounded retry schedule for the direct insert path (seconds, before jitter).
_RETRY_DELAYS = (0.2, 0.8, 1.8)

# Best-effort run/org attribution for calls made inside a run's request context.
# Set once per run at admission (api/chat.py) next to the existing model binding;
# tasks created from that context inherit it. `None` is an honest value for
# non-run calls (e.g. knowledge ingest embeddings).
_run_ctx: ContextVar[tuple[str | None, str | None]] = ContextVar(
    "aegisops_ledger_run_ctx", default=(None, None)
)

# Strong references to in-flight persistence tasks: the event loop keeps only a
# weak ref, so an un-referenced task can be garbage-collected before it runs —
# losing the record without ever reaching the spill journal.
_pending_tasks: set[asyncio.Task] = set()


def bind_run(run_id: str | None, org_id: str | None) -> None:
    """Attribute subsequent LLM calls in this context to a run/org (accounting only)."""
    _run_ctx.set((run_id, org_id))


def _tok(usage: dict | None, *keys: str) -> int:
    if not usage:
        return 0
    for k in keys:
        v = usage.get(k)
        if isinstance(v, (int, float)) and v >= 0:
            return int(v)
    return 0


def _cost_usd(settings: Settings, input_tokens: int, output_tokens: int) -> float | None:
    if not input_tokens and not output_tokens:
        return None
    return round(
        input_tokens / 1_000_000 * settings.gemini_cost_per_1m_input
        + output_tokens / 1_000_000 * settings.gemini_cost_per_1m_output,
        8,
    )


def record_usage(
    settings: Settings,
    *,
    purpose: str,
    model: str,
    usage: dict | None = None,
    latency_ms: int | None = None,
    outcome: str = "ok",
    agent_kind: str = "main",
    requested_model: str | None = None,
    prompt_version: str | None = None,
    provider: str = "google",
    run_id: str | None = None,
    org_id: str | None = None,
) -> dict[str, Any]:
    """Create one authoritative usage record and hand it to the durable-delivery path.

    Never raises and never blocks the caller's response path. Returns the record
    (tests and callers may use the id). Failures end in the spill journal, loudly.
    """
    ctx_run, ctx_org = _run_ctx.get()
    input_tokens = _tok(usage, "input", "input_tokens", "prompt_tokens")
    output_tokens = _tok(usage, "output", "output_tokens", "completion_tokens")
    total = _tok(usage, "total", "total_tokens") or (input_tokens + output_tokens)
    row: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "run_id": run_id or ctx_run,
        "org_id": org_id or ctx_org,
        "task_id": None,  # Task entity arrives with the durable run system (P2/P3)
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "requested_model": requested_model,
        "agent_kind": agent_kind,
        "prompt_version": prompt_version,  # populated once the prompt registry lands (P2)
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "cost_usd": _cost_usd(settings, input_tokens, output_tokens),
        "latency_ms": latency_ms,
        "outcome": outcome[:60],
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop (sync context / interpreter teardown): spill directly —
        # durable now, replayed later. Never lost.
        _spill(row, settings)
        return row
    task = loop.create_task(_persist_with_retry(row, settings))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return row


async def _persist_with_retry(row: dict[str, Any], settings: Settings) -> None:
    last: Exception | None = None
    for delay in (*_RETRY_DELAYS, None):
        try:
            await _insert(row)
            _metric("records", path="direct")
            return
        except Exception as exc:  # noqa: BLE001 — every failure kind ends in the spill path
            last = exc
            if delay is not None:
                await asyncio.sleep(delay * (1.0 + random.random() * 0.5))
    # Off-thread: the spill append fsyncs — never block the event loop for it.
    await asyncio.to_thread(_spill, row, settings)
    _metric("spill")
    log.error("ledger.record_spilled", record_id=row["id"], purpose=row["purpose"],
              error=str(last)[:200])


async def _insert(row: dict[str, Any]) -> None:
    """Idempotent insert: ON CONFLICT (id) DO NOTHING — safe for retry AND replay."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from ..db.models import LlmUsage
    from ..db.session import get_sessionmaker

    values = dict(row)
    values["id"] = uuid.UUID(values["id"])
    values["ts"] = datetime.fromisoformat(values["ts"])
    for key in ("run_id", "org_id", "task_id"):
        values[key] = uuid.UUID(values[key]) if values.get(key) else None

    maker = get_sessionmaker()
    async with maker() as session:
        await session.execute(
            pg_insert(LlmUsage.__table__).values(**values).on_conflict_do_nothing(
                index_elements=["id"])
        )
        await session.commit()


def spill_path(settings: Settings) -> Path:
    return Path(getattr(settings, "aegisops_ledger_spill_path", "./llm_usage_spill.jsonl"))


def _spill(row: dict[str, Any], settings: Settings) -> None:
    """Append one record to the local durable journal (fsync'd; ids/tokens only)."""
    try:
        path = spill_path(settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":"), default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001 — the last resort failed; scream, never crash the run
        log.error("ledger.spill_write_failed", record_id=row.get("id"), error=str(exc)[:200])


def _metric(kind: str, *, path: str | None = None, n: int = 1) -> None:
    try:
        from .. import metrics as m

        if kind == "spill":
            m.LEDGER_SPILL.inc(n)
        else:
            m.LEDGER_RECORDS.labels(path=path or "direct").inc(n)
    except Exception:  # noqa: BLE001 — metrics must never break accounting
        pass


async def replay_spill(settings: Settings) -> dict[str, int]:
    """Replay spilled records into PostgreSQL. Idempotent via the record id PK.

    Called from the reconciler sweep loop (worker role). Records that still fail
    remain in the journal for the next pass; replayed lines are removed atomically.
    """
    path = spill_path(settings)
    if not path.exists():
        return {"replayed": 0, "remaining": 0}
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except Exception as exc:  # noqa: BLE001
        log.error("ledger.spill_read_failed", error=str(exc)[:200])
        return {"replayed": 0, "remaining": -1}
    if not lines:
        return {"replayed": 0, "remaining": 0}

    remaining: list[str] = []
    replayed = 0
    for ln in lines:
        try:
            await _insert(json.loads(ln))
            replayed += 1
        except Exception:  # noqa: BLE001 — keep the line; next sweep retries
            remaining.append(ln)

    def _rewrite() -> None:  # atomic temp+rename, fsync'd — off the event loop
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("".join(f"{ln}\n" for ln in remaining))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if not remaining:
            try:
                path.unlink()
            except OSError:
                pass

    await asyncio.to_thread(_rewrite)
    if replayed:
        _metric("records", path="replay", n=replayed)
        log.info("ledger.spill_replayed", replayed=replayed, remaining=len(remaining))
    return {"replayed": replayed, "remaining": len(remaining)}
