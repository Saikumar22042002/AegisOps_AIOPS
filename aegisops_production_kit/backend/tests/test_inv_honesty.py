"""Inventory honesty — `unreachable` marking (live-acceptance follow-up, owner item 4).

Sandbox accounts rotate per credential set, stranding inventory rows whose real resources
we can no longer reach (live: `accept-web3-net`, `acc-web-net`, `aegis-accept-b3`,
`aegis-accept-b1`, `accept-ec2-net`). An `active` row is offered as a DEP parent and a
day-2 target and blocks its name — so a row we cannot reach must be flipped to
`unreachable`: explicit per-name, reason + timestamp recorded on the row, logged, and
REVERSIBLE (--undo). Never a bulk sweep, never silent. Uses live Postgres; rows are
namespaced `itest-unrch-` and cleaned before/after.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from app.agents import inventory, templates
from app.db.models import Resource
from app.db.session import session_scope

_PREFIX = "itest-unrch-"


async def _cleanup(org: str) -> None:
    async with session_scope() as s:
        await s.execute(sa.delete(Resource).where(
            Resource.org_id == uuid.UUID(org), Resource.name.like(f"{_PREFIX}%")))


@pytest.fixture
async def clean_rows(org_id):
    await _cleanup(org_id)
    yield org_id
    await _cleanup(org_id)


async def _record(org: str, name: str) -> None:
    t = templates.select("aws", "vpc")
    state = {"org_id": org, "session_id": None, "run_id": None,
             "parsed_inputs": {"name": name}}
    await inventory.record_from_apply(state, t, {"vpc_id": f"vpc-{name[-4:]}"})


async def test_mark_unreachable_removes_from_active_and_records_why(clean_rows):
    org = clean_rows
    name = _PREFIX + "net"
    await _record(org, name)
    assert any(r["name"] == name for r in await inventory.list_active(org))

    out = await inventory.mark_unreachable(org, name, "sandbox account rotated (run-1 creds)")
    assert out is not None and out["status"] == "unreachable"
    assert out["reason"] == "sandbox account rotated (run-1 creds)"
    # gone from every active surface (DEP candidates, day-2 targets, name-duplicate checks)
    assert not any(r["name"] == name for r in await inventory.list_active(org))
    # the row itself persists for audit, with the reason + timestamp on it
    async with session_scope() as s:
        row = (await s.execute(sa.select(Resource).where(
            Resource.org_id == uuid.UUID(org), Resource.name == name))).scalars().one()
        assert row.status == "unreachable"
        assert row.attributes["unreachable_reason"].startswith("sandbox account rotated")
        assert row.attributes["unreachable_marked_at"]


async def test_undo_restores_active_untouched(clean_rows):
    org = clean_rows
    name = _PREFIX + "undo"
    await _record(org, name)
    await inventory.mark_unreachable(org, name, "rotated")
    out = await inventory.mark_unreachable(org, name, "", undo=True)
    assert out is not None and out["status"] == "active" and out["reason"] is None
    active = [r for r in await inventory.list_active(org) if r["name"] == name]
    assert len(active) == 1
    assert "unreachable_reason" not in active[0]["attributes"]      # stamps removed on undo


async def test_never_guesses(clean_rows):
    org = clean_rows
    # unknown name → None (marks nothing)
    assert await inventory.mark_unreachable(org, _PREFIX + "ghost", "x") is None
    # a DESTROYED row is not markable — only silently-active rows are the honesty problem
    name = _PREFIX + "gone"
    await _record(org, name)
    async with session_scope() as s:
        row = (await s.execute(sa.select(Resource).where(
            Resource.org_id == uuid.UUID(org), Resource.name == name))).scalars().one()
        row.status = "destroyed"
    assert await inventory.mark_unreachable(org, name, "x") is None
    # undo with nothing unreachable → None
    assert await inventory.mark_unreachable(org, name, "", undo=True) is None


async def test_admin_cli_marks_and_undoes(clean_rows, capsys):
    # the command coroutine directly (admin.main wraps it in asyncio.run, which can't nest
    # inside the test loop) — arg parsing lives in the coroutine, so it's fully covered
    from app import admin
    org = clean_rows
    name = _PREFIX + "cli"
    await _record(org, name)

    rc = await admin._mark_unreachable([name, "--reason", "sandbox rotated", "--org", org])
    assert rc == 0
    assert '"unreachable"' in capsys.readouterr().out
    assert not any(r["name"] == name for r in await inventory.list_active(org))

    rc = await admin._mark_unreachable([name, "--undo", "--org", org])
    assert rc == 0
    assert any(r["name"] == name for r in await inventory.list_active(org))


async def test_admin_cli_requires_a_reason(clean_rows, capsys):
    from app import admin
    rc = await admin._mark_unreachable([_PREFIX + "x", "--org", clean_rows])
    assert rc == 2
    assert "--reason" in capsys.readouterr().err


async def test_admin_cli_unknown_row_is_rc1(clean_rows, capsys):
    from app import admin
    rc = await admin._mark_unreachable([_PREFIX + "ghost", "--reason", "x", "--org", clean_rows])
    assert rc == 1
    assert "no active row" in capsys.readouterr().err


def test_admin_usage_lists_the_command(capsys):
    from app import admin
    assert admin.main([]) == 2
    assert "mark-unreachable" in capsys.readouterr().err
