"""MPP — Module Promotion Pipeline: draft → REAL fmt/validate → scan seam → propose → human
review → promote|reject. A drafted module is inert data — unselectable until promoted, and
generation never executes in the same turn. Promotion fails CLOSED without a passed scan.

Integration: live Postgres for the proposal rows; real `terraform fmt/validate` in isolation.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete

from app.agents import module_pipeline as mpp
from app.agents import templates
from app.agents.module_pipeline import ModulePipelineError
from app.db.models import ModuleProposal
from app.db.session import session_scope
from app.settings import get_settings

_GOOD_TF = '''terraform {
  required_providers {
    null = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

variable "name" {
  type = string
}

resource "null_resource" "this" {
  triggers = {
    name = var.name
  }
}
'''

_BROKEN_TF = 'resource "null_resource" "this" {\n  triggers = {\n'          # unclosed braces
_UNFORMATTED_TF = 'variable "name" {\n      type    =     string\n}\n'      # fmt -check fails


@pytest.fixture
async def clean_registry():
    added_before = set(templates._PROMOTED)
    yield
    for key in set(templates._PROMOTED) - added_before:
        templates._PROMOTED.pop(key, None)


async def _cleanup_proposals(org_id: str):
    async with session_scope() as s:
        await s.execute(delete(ModuleProposal).where(ModuleProposal.org_id == uuid.UUID(org_id)))


# ── draft validation + inertness ───────────────────────────────────────────────────────────

async def test_draft_validation_rejects_bad_shapes(live_db, throwaway_org):
    org = throwaway_org
    try:
        with pytest.raises(ModulePipelineError):
            await mpp.draft(org, "not-a-key", {"main.tf": _GOOD_TF})
        with pytest.raises(ModulePipelineError):
            await mpp.draft(org, "aws.ec2", {"main.tf": _GOOD_TF})  # exists in the catalog
        with pytest.raises(ModulePipelineError):
            await mpp.draft(org, "aws.efs", {})                     # no files
        with pytest.raises(ModulePipelineError):
            await mpp.draft(org, "aws.efs", {"../evil.tf": _GOOD_TF})  # path escape
    finally:
        await _cleanup_proposals(org)


async def test_draft_is_inert_and_unselectable_until_promoted(live_db, throwaway_org,
                                                              clean_registry):
    """Acceptance: drafted module unselectable until promoted; never same-turn execution —
    drafting stores data and nothing else (no template registration, no plan, no run)."""
    org = throwaway_org
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _GOOD_TF}, description="EFS draft")
        assert pid
        assert templates.by_key("aws.efs") is None            # NOT selectable
        assert templates.select("aws", "efs") is None         # NOT selectable by cloud/resource
        # …and proposing it (even with green checks) still does not make it selectable.
        await mpp.run_checks(pid)
        await mpp.propose(pid)
        assert templates.by_key("aws.efs") is None
    finally:
        await _cleanup_proposals(org)


# ── real checks ────────────────────────────────────────────────────────────────────────────

async def test_run_checks_real_terraform_green_module(live_db, throwaway_org):
    org = throwaway_org
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _GOOD_TF})
        res = await mpp.run_checks(pid)
        assert res["fmt_ok"] is True
        assert res["validate_ok"] is True
        # Environment-dependent: a real scan (passed/failed) when checkov/tfsec is
        # installed (the image bakes checkov in), honestly `unavailable` otherwise.
        assert res["scan"]["status"] in ("unavailable", "passed", "failed")
    finally:
        await _cleanup_proposals(org)


async def test_run_checks_flags_broken_and_unformatted_hcl(live_db, throwaway_org):
    org = throwaway_org
    try:
        broken = await mpp.draft(org, "aws.efs", {"main.tf": _BROKEN_TF})
        res = await mpp.run_checks(broken)
        assert res["validate_ok"] is False

        ugly = await mpp.draft(org, "gcp.pubsub", {"main.tf": _UNFORMATTED_TF})
        res2 = await mpp.run_checks(ugly)
        assert res2["fmt_ok"] is False
    finally:
        await _cleanup_proposals(org)


async def test_propose_requires_green_checks(live_db, throwaway_org):
    org = throwaway_org
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _BROKEN_TF})
        await mpp.run_checks(pid)
        with pytest.raises(ModulePipelineError):
            await mpp.propose(pid)
    finally:
        await _cleanup_proposals(org)


# ── review: fail-closed promotion / rejection ──────────────────────────────────────────────

async def test_promotion_is_blocked_without_a_passed_scan(live_db, throwaway_org, monkeypatch):
    """Fail closed: scanner unavailable → scan 'unavailable' → promotion refused.
    Forced via the seam so the behavior is tested regardless of what the image installs
    (the API image now bakes checkov in — the environment can no longer prove this path)."""
    org = throwaway_org
    monkeypatch.setattr(mpp, "_scan_command", lambda workdir: None)
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _GOOD_TF})
        await mpp.run_checks(pid)          # scan: unavailable (seam forced)
        await mpp.propose(pid)
        with pytest.raises(ModulePipelineError, match="PASSED security scan"):
            await mpp.review(pid, "promote", reviewer="maya")
    finally:
        await _cleanup_proposals(org)


_LEAKY_TF = '''terraform {
  required_providers {
    null = { source = "hashicorp/null", version = "~> 3.2" }
  }
}

resource "null_resource" "leak" {
  triggers = {
    aws_access_key = "AKIAIOSFODNN7EXAMPLE"
    aws_secret     = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
  }
}
'''  # the canonical AWS documentation EXAMPLE credential — non-functional, published by AWS


@pytest.mark.skipif(not (shutil.which("checkov") or shutil.which("tfsec")),
                    reason="no scanner installed in this environment")
async def test_real_scan_gates_promotion_end_to_end(live_db, throwaway_org):
    """DLV-13, automated: a draft carrying an embedded credential really FAILS the scan
    (finding text lands in the scan detail) and promotion is refused; a clean draft
    really PASSES. Runs the actual scanner baked into the image — no seams."""
    org = throwaway_org
    try:
        leaky = await mpp.draft(org, "aws.secretleak", {"main.tf": _LEAKY_TF})
        res = await mpp.run_checks(leaky)
        assert res["fmt_ok"] is True and res["validate_ok"] is True
        assert res["scan"]["status"] == "failed"
        assert res["scan"]["tool"] in ("checkov", "tfsec")
        if res["scan"]["tool"] == "checkov":
            assert "CKV_SECRET" in res["scan"]["detail"]
        await mpp.propose(leaky)               # proposable (humans may see it) …
        with pytest.raises(ModulePipelineError, match="PASSED security scan"):
            await mpp.review(leaky, "promote", reviewer="maya")   # … but never the library

        clean = await mpp.draft(org, "aws.cleandraft", {"main.tf": _GOOD_TF})
        res2 = await mpp.run_checks(clean)
        assert res2["scan"]["status"] == "passed"
    finally:
        await _cleanup_proposals(org)


async def test_promote_registers_the_module_and_materializes_it(live_db, throwaway_org,
                                                                clean_registry, monkeypatch,
                                                                tmp_path):
    org = throwaway_org
    # A passing scanner (seam) + an isolated workspaces dir so the repo is never touched.
    monkeypatch.setattr(mpp, "_scan_command", lambda workdir: ["sh", "-c", "exit 0"])
    settings = get_settings().model_copy(update={"terraform_workspaces_dir": str(tmp_path)})
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _GOOD_TF}, description="EFS module")
        await mpp.run_checks(pid)
        await mpp.propose(pid)
        out = await mpp.review(pid, "promote", reviewer="maya", settings=settings)
        assert out["status"] == "promoted"

        # NOW it joins the approved library…
        t = templates.by_key("aws.efs")
        assert t is not None and t.workspace == "promoted-aws-efs"
        assert templates.select("aws", "efs") is t
        # …with the module materialized on disk…
        assert (Path(str(tmp_path)) / "promoted-aws-efs" / "main.tf").read_text(encoding="utf-8") == _GOOD_TF
        # …a permissive-but-named input schema, and honest policy rows.
        validated = t.schema(name="shared-fs", extra_knob="x").model_dump()
        assert validated["name"] == "shared-fs" and validated["extra_knob"] == "x"
        assert t.policy_fn({})[0]["evaluated"] is False
        # Terminal: checks are frozen, re-review refused.
        with pytest.raises(ModulePipelineError):
            await mpp.run_checks(pid)
        with pytest.raises(ModulePipelineError):
            await mpp.review(pid, "reject", reviewer="maya", settings=settings)
    finally:
        await _cleanup_proposals(org)


async def test_reject_is_terminal(live_db, throwaway_org, clean_registry):
    org = throwaway_org
    try:
        pid = await mpp.draft(org, "aws.efs", {"main.tf": _GOOD_TF})
        await mpp.run_checks(pid)
        await mpp.propose(pid)
        out = await mpp.review(pid, "reject", reviewer="maya", note="not needed")
        assert out["status"] == "rejected"
        assert templates.by_key("aws.efs") is None
        with pytest.raises(ModulePipelineError):
            await mpp.review(pid, "promote", reviewer="maya")
    finally:
        await _cleanup_proposals(org)


async def test_rehydrate_reregisters_promoted_modules(live_db, throwaway_org, clean_registry,
                                                      monkeypatch, tmp_path):
    org = throwaway_org
    monkeypatch.setattr(mpp, "_scan_command", lambda workdir: ["sh", "-c", "exit 0"])
    settings = get_settings().model_copy(update={"terraform_workspaces_dir": str(tmp_path)})
    try:
        pid = await mpp.draft(org, "azure.files", {"main.tf": _GOOD_TF})
        await mpp.run_checks(pid)
        await mpp.propose(pid)
        await mpp.review(pid, "promote", reviewer="maya", settings=settings)
        templates._PROMOTED.pop("azure.files")            # simulate a fresh process
        assert templates.by_key("azure.files") is None
        count = await mpp.rehydrate_promoted(settings)
        assert count >= 1 and templates.by_key("azure.files") is not None
    finally:
        await _cleanup_proposals(org)
