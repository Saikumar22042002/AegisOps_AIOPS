"""PR-5 BACKUP — Postgres is the only must-back-up store; Neo4j is a derived mirror proven
by `rebuild_world_model` (rebuilds the live graph from inventory, no cloud read). The pg_dump
script + RESTORE_RUNBOOK exist; the live restore drill is DEFERRED (DLV-35)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest


def _find(rel: str) -> Path:
    from app import metrics
    backend = Path(metrics.__file__).resolve().parents[1]   # backend/ (host) or /app (container)
    for base in (backend, backend.parent):
        p = base / rel
        if p.exists():
            return p
    raise FileNotFoundError(rel)


def test_backup_script_and_runbook_shipped():
    script = _find("infra/backup/pg_backup.sh")
    runbook = _find("docs/RESTORE_RUNBOOK.md")
    assert script.is_file() and runbook.is_file()
    s = script.read_text(encoding="utf-8")
    assert "pg_dump" in s and "--format=custom" in s and "RETENTION" in s
    rb = runbook.read_text(encoding="utf-8")
    # the derived-mirror claim + the rebuild command are documented
    assert "rebuild-world-model" in rb and "pg_restore" in rb
    assert "audit_log" in rb and "never" in rb.lower()


def test_admin_cli_exposes_the_rebuild_command():
    from app import admin
    assert "rebuild-world-model" in admin._COMMANDS
    assert "retention-sweep" in admin._COMMANDS
    assert admin.main(["nonexistent"]) == 2       # honest usage error, no crash


async def test_rebuild_world_model_from_inventory(live_db, live_neo4j, throwaway_org):
    """Wipe the org's world-model nodes, then rebuild PURELY from Postgres inventory — the
    node reappears with no cloud read (proves Neo4j is rebuildable)."""
    from app.db.models import Resource
    from app.db.session import session_scope
    from app.graph_db import world_model
    from sqlalchemy import delete

    org = throwaway_org
    async with session_scope() as s:
        s.add(Resource(org_id=uuid.UUID(org), name="rebuild-me", cloud="aws",
                       resource_type="vpc", workspace="aws-vpc", provider_id="vpc-reb",
                       status="active", inputs={"name": "rebuild-me"},
                       attributes={"vpc_id": "vpc-reb"}))
    try:
        # start from an empty graph for this org
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
        before = await world_model.list_active(org)
        assert not any(r["name"] == "rebuild-me" for r in before)

        out = await world_model.rebuild_from_inventory()
        assert out["resources"] >= 1

        after = await world_model.list_active(org)
        assert any(r["name"] == "rebuild-me" for r in after)   # rebuilt from inventory alone
    finally:
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))
        await world_model._run("MATCH (r:Resource {org_id:$o}) DETACH DELETE r", o=org)
