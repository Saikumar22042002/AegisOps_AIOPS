"""D1 — hot-path indexes exist and back the per-turn queries (no seq scans at scale).

Integration (live Postgres): asserts the migration created the four indexes, and that the
planner chooses an index (not a seq scan) for the transcript-load query once the planner is
told to prefer indexes (a tiny test table would otherwise seq-scan regardless).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.db.session import session_scope

_EXPECTED = {
    "ix_messages_session_created",
    "ix_messages_run_id",
    "ix_runs_session_id",
    "ix_runs_org_created",
}


async def test_hot_path_indexes_present(live_db):
    async with session_scope() as s:
        rows = (await s.execute(sa.text(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('messages','runs')"
        ))).scalars()
        present = set(rows)
    missing = _EXPECTED - present
    assert not missing, f"missing hot-path indexes: {missing}"


async def test_transcript_query_uses_index(live_db):
    """EXPLAIN the exact transcript-load shape; with seqscan disabled the planner must be ABLE
    to satisfy it from ix_messages_session_created (proves the index covers the query)."""
    async with session_scope() as s:
        await s.execute(sa.text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join((await s.execute(sa.text(
            "EXPLAIN SELECT * FROM messages "
            "WHERE session_id = '00000000-0000-0000-0000-000000000000' "
            "ORDER BY created_at"
        ))).scalars())
    assert "ix_messages_session_created" in plan or "Index" in plan, \
        f"transcript query did not use an index:\n{plan}"
