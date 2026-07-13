"""PR-1 TFHYGIENE — Terraform disk lifecycle: terminal runs leave no .tfplan behind
(the reviewable record persists in runs.plan_json); the reconciler sweeps strays >7d and
prunes DESTROYED resources' empty state workspaces (sweeper-only, logged, never inline).
Plugin cache + .gitignore posture verified."""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pytest

from app.settings import get_settings
from app.tools import terraform as tf_tools


@pytest.fixture
def fake_ws(tmp_path, monkeypatch):
    """A throwaway terraform-workspaces tree; settings point at it for the duration."""
    settings = get_settings()
    monkeypatch.setattr(settings, "terraform_workspaces_dir", str(tmp_path), raising=False)
    (tmp_path / "aws-ec2").mkdir()
    (tmp_path / "aws-s3").mkdir()
    return tmp_path, settings


def test_terminal_cleanup_removes_only_the_runs_plan_files(fake_ws):
    tmp, settings = fake_ws
    rid, other = str(uuid.uuid4()), str(uuid.uuid4())
    mine1 = tmp / "aws-ec2" / f"aegisops-res-web-{rid}.tfplan"
    mine2 = tmp / "aws-s3" / f"aegisops-default-{rid}.tfplan"
    theirs = tmp / "aws-ec2" / f"aegisops-res-web-{other}.tfplan"
    for p in (mine1, mine2, theirs):
        p.write_text("x")
    assert tf_tools.remove_run_plan_files(settings, rid) == 2
    assert not mine1.exists() and not mine2.exists()
    assert theirs.exists()                                  # someone else's run untouched
    assert tf_tools.remove_run_plan_files(settings, rid) == 0   # idempotent


def test_stray_sweep_removes_only_old_unprotected_files(fake_ws):
    tmp, settings = fake_ws
    waiting_rid = str(uuid.uuid4())
    old = tmp / "aws-ec2" / "aegisops-res-x-dead.tfplan"
    fresh = tmp / "aws-ec2" / "aegisops-res-y-live.tfplan"
    waiting = tmp / "aws-ec2" / f"aegisops-res-z-{waiting_rid}.tfplan"
    for p in (old, fresh, waiting):
        p.write_text("x")
    stale = time.time() - 8 * 86400
    os.utime(old, (stale, stale))
    os.utime(waiting, (stale, stale))          # ALSO old — but its run still awaits approval
    removed = tf_tools.sweep_stray_plan_files(settings, max_age_days=7,
                                              keep_run_ids={waiting_rid})
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()
    assert waiting.exists()                    # BINDING: awaiting_approval is never stray


def test_prune_refuses_a_nonempty_state_and_prunes_an_empty_one(fake_ws):
    tmp, settings = fake_ws
    busy = tmp / "aws-ec2" / "terraform.tfstate.d" / "res-busy"
    busy.mkdir(parents=True)
    (busy / "terraform.tfstate").write_text(json.dumps(
        {"resources": [{"type": "aws_instance"}]}))
    assert tf_tools.prune_destroyed_state_workspace(settings, "aws-ec2", "res-busy") is False
    assert busy.exists()                                    # NEVER prune real state

    empty = tmp / "aws-ec2" / "terraform.tfstate.d" / "res-gone"
    empty.mkdir(parents=True)
    (empty / "terraform.tfstate").write_text(json.dumps({"resources": []}))
    assert tf_tools.prune_destroyed_state_workspace(settings, "aws-ec2", "res-gone") is True
    assert not empty.exists()
    # missing dir → honest no-op
    assert tf_tools.prune_destroyed_state_workspace(settings, "aws-ec2", "res-gone") is False


async def test_reconciler_hygiene_pass_prunes_old_destroyed_rows(fake_ws, live_db,
                                                                 live_redis, throwaway_org):
    """The sweep prunes a destroyed row's empty state workspace past the threshold and
    clears the row's pointer — and refuses everything younger or busier."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete, select, update

    from app.agents.reconciler import Reconciler
    from app.db.models import Resource
    from app.db.session import session_scope

    tmp, settings = fake_ws
    slug = "res-oldgone"
    d = tmp / "aws-ec2" / "terraform.tfstate.d" / slug
    d.mkdir(parents=True)
    (d / "terraform.tfstate").write_text(json.dumps({"resources": []}))

    org = throwaway_org
    async with session_scope() as s:
        s.add(Resource(org_id=uuid.UUID(org), name="oldgone", cloud="aws",
                       resource_type="ec2", workspace="aws-ec2", state_workspace=slug,
                       provider_id="i-old", status="destroyed", inputs={}))
    async with session_scope() as s:   # backdate past the 7-day threshold
        await s.execute(update(Resource).where(Resource.org_id == uuid.UUID(org)).values(
            updated_at=datetime.now(timezone.utc) - timedelta(days=8)))

    try:
        rec = Reconciler()
        out = await rec.sweep_tf_hygiene(max_age_days=7)
        assert out["state_workspaces_pruned"] >= 1
        assert not d.exists()
        async with session_scope() as s:
            row = (await s.execute(select(Resource).where(
                Resource.org_id == uuid.UUID(org)))).scalars().one()
            assert row.state_workspace is None              # pointer cleared with the dir
    finally:
        async with session_scope() as s:
            await s.execute(delete(Resource).where(Resource.org_id == uuid.UUID(org)))


def test_plan_cleanup_is_wired_into_the_terminal_path():
    """The single terminal choke points call the cleanup — pin it at the source."""
    src = (Path(tf_tools.__file__).resolve().parents[2] / "app" / "api" / "chat.py"
           ).read_text(encoding="utf-8")
    assert "_cleanup_terminal_plan_files(run_id)" in src
    assert 'status_ in ("completed", "failed")' in src      # awaiting_approval keeps its plan


def test_plugin_cache_and_gitignore_posture():
    settings = get_settings()
    assert getattr(settings, "tf_plugin_cache_dir", None) is not None
    gi_path = None
    for base in Path(__file__).resolve().parents:            # host layout, or the ro-mount
        if (base / ".gitignore").is_file():
            gi_path = base / ".gitignore"
            break
    assert gi_path is not None, ".gitignore not reachable (api-test mounts ./.gitignore)"
    gi = gi_path.read_text(encoding="utf-8")
    assert ".terraform/" in gi and "*.tfplan" in gi and "*.tfstate" in gi