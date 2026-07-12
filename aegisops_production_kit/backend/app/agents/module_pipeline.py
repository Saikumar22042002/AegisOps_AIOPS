"""Module Promotion Pipeline (MPP).

When no approved module exists, a new Terraform module may be DRAFTED — but it is inert data
until a human promotes it. Locked decision 11: **generation and execution never happen in the
same turn**; only after promotion does a module join the approved library.

Pipeline: `draft` (store the files — LLM-generated or operator-provided; nothing is planned or
applied) → `run_checks` (REAL `terraform fmt -check` + `terraform init -backend=false` +
`terraform validate` in an isolated scratch dir, plus a security scan through the scanner seam:
checkov or tfsec when installed, otherwise honestly `unavailable`) → `propose` (requires green
checks) → `review` (approver RBAC): **promote** — requires fmt+validate green AND a PASSED scan
(fail closed: an unscanned draft cannot be promoted) — materializes the module under the
Terraform workspaces dir and registers a runtime `WorkflowTemplate`, or **reject** (terminal).

Promoted modules use a permissive input schema (`PromotedModuleInputs`: `name` required, extra
keys pass through as -var) and `_todo` policy rows — the plan-guard, approval gate, and state
isolation apply to them exactly as to the built-in catalog.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import structlog

from ..db.models import ModuleProposal
from ..db.session import session_scope
from ..schemas import workflows as wf
from ..settings import Settings, get_settings
from ..tools.console import CommandConsole
from . import templates

log = structlog.get_logger(__name__)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")  # e.g. aws.efs
_FILENAME_RE = re.compile(r"^[\w.-]+\.tf$")


class PromotedModuleInputs(wf.WorkflowInputs):
    """Inputs for a promoted (dynamically registered) module: `name` required; other keys
    pass through to Terraform as -var. The plan-guard/approval/policy machinery still applies."""

    model_config = {"extra": "allow"}
    name: str


class ModulePipelineError(Exception):
    pass


def _todo_policy(i: dict, resources=None) -> list[dict]:
    return [{"name": "Promoted module — org review is the policy record", "passed": None,
             "evaluated": False, "detail": "reviewed + promoted by a human; add predicates as needed"}]


def _validate_draft(key: str, files: dict) -> None:
    if not _KEY_RE.match(key or ""):
        raise ModulePipelineError(f"module key '{key}' must look like '<cloud>.<resource>'")
    if templates.by_key(key) is not None:
        raise ModulePipelineError(f"'{key}' already exists in the approved catalog")
    if not files or not any(f.endswith(".tf") for f in files):
        raise ModulePipelineError("a draft needs at least one .tf file")
    for fname in files:
        if not _FILENAME_RE.match(fname):
            raise ModulePipelineError(f"illegal draft filename '{fname}' (flat *.tf only)")


async def draft(org_id: str, key: str, files: dict[str, str], *, description: str = "",
                created_by: str = "") -> str:
    """Store a drafted module. NOTHING is planned or applied — the draft is data, unselectable
    until a human promotes it (never same-turn execution). Returns the proposal id."""
    _validate_draft(key, files)
    cloud, resource = key.split(".", 1)
    async with session_scope() as s:
        row = ModuleProposal(org_id=uuid.UUID(org_id), key=key, cloud=cloud, resource=resource,
                             description=description or None, files=files,
                             created_by=created_by or None, status="draft")
        s.add(row)
        await s.flush()
        pid = str(row.id)
    log.info("mpp.drafted", proposal=pid, key=key, files=list(files))
    return pid


def _scan_command(workdir: str) -> list[str] | None:
    """The security scanner invocation, preferring checkov then tfsec — None when neither is
    installed (the scan is then honestly `unavailable`, which BLOCKS promotion; fail closed)."""
    if shutil.which("checkov"):
        return ["checkov", "--directory", workdir, "--quiet", "--compact", "-o", "cli"]
    if shutil.which("tfsec"):
        return ["tfsec", workdir, "--no-color", "--concise-output"]
    return None


async def run_checks(proposal_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """REAL checks over the draft in an isolated scratch dir: `terraform fmt -check`,
    `terraform init -backend=false` + `terraform validate`, then the security scan seam."""
    settings = settings or get_settings()
    async with session_scope() as s:
        row = await s.get(ModuleProposal, uuid.UUID(proposal_id))
        if row is None:
            raise ModulePipelineError("proposal not found")
        if row.status in ("promoted", "rejected"):
            raise ModulePipelineError(f"proposal is terminal ({row.status}) — checks are frozen")
        files = dict(row.files)

    workdir = Path("/tmp") / f"mpp-{proposal_id[:8]}"
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True)
    try:
        for fname, content in files.items():
            (workdir / fname).write_text(content, encoding="utf-8")

        console = CommandConsole(cwd=str(workdir), env={"TF_IN_AUTOMATION": "1"}, timeout=180)
        tf = settings.terraform_bin
        fmt = await console.run([tf, "fmt", "-check", "-diff"])
        fmt_ok = fmt.returncode == 0
        init = await console.run([tf, "init", "-backend=false", "-input=false", "-no-color"])
        validate = await console.run([tf, "validate", "-no-color"]) if init.returncode == 0 else init
        validate_ok = init.returncode == 0 and validate.returncode == 0
        validate_detail = "\n".join((validate.stderr or validate.stdout)[-8:])[:800]

        cmd = _scan_command(str(workdir))
        if cmd is None:
            scan = {"tool": None, "status": "unavailable",
                    "detail": "no scanner installed (checkov/tfsec) — promotion is blocked "
                              "until a scan passes (fail closed)"}
        else:
            res = await console.run(cmd)
            scan = {"tool": cmd[0], "status": "passed" if res.returncode == 0 else "failed",
                    "detail": "\n".join((res.stdout + res.stderr)[-20:])[:2000]}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    async with session_scope() as s:
        row = await s.get(ModuleProposal, uuid.UUID(proposal_id))
        row.fmt_ok, row.validate_ok, row.scan = fmt_ok, validate_ok, scan
    log.info("mpp.checked", proposal=proposal_id, fmt_ok=fmt_ok, validate_ok=validate_ok,
             scan_status=scan["status"])
    return {"fmt_ok": fmt_ok, "validate_ok": validate_ok, "validate_detail": validate_detail,
            "scan": scan}


async def propose(proposal_id: str) -> None:
    """draft → proposed. Requires fmt + validate green (the scan gates PROMOTION, so a
    proposal with an unavailable scanner can still reach human eyes — but never the library)."""
    async with session_scope() as s:
        row = await s.get(ModuleProposal, uuid.UUID(proposal_id))
        if row is None:
            raise ModulePipelineError("proposal not found")
        if row.status != "draft":
            raise ModulePipelineError(f"only a draft can be proposed (status={row.status})")
        if not (row.fmt_ok and row.validate_ok):
            raise ModulePipelineError("run_checks must pass fmt + validate before proposing")
        row.status = "proposed"
    log.info("mpp.proposed", proposal=proposal_id)


async def review(proposal_id: str, decision: str, *, reviewer: str, note: str = "",
                 settings: Settings | None = None) -> dict[str, Any]:
    """The human gate: promote | reject. Promotion REQUIRES green fmt/validate AND a PASSED
    security scan (an `unavailable` scan blocks — fail closed), then materializes the module
    and registers it into the approved library."""
    if decision not in ("promote", "reject"):
        raise ModulePipelineError("decision must be 'promote' or 'reject'")
    settings = settings or get_settings()
    async with session_scope() as s:
        row = await s.get(ModuleProposal, uuid.UUID(proposal_id))
        if row is None:
            raise ModulePipelineError("proposal not found")
        if row.status != "proposed":
            raise ModulePipelineError(f"only a proposed module can be reviewed (status={row.status})")
        if decision == "reject":
            row.status, row.reviewed_by, row.review_note = "rejected", reviewer, note or None
            log.info("mpp.rejected", proposal=proposal_id, reviewer=reviewer)
            return {"status": "rejected"}
        if not (row.fmt_ok and row.validate_ok):
            raise ModulePipelineError("promotion requires green fmt + validate")
        if (row.scan or {}).get("status") != "passed":
            raise ModulePipelineError(
                "promotion requires a PASSED security scan — "
                f"current: {(row.scan or {}).get('status', 'not run')} (fail closed)")
        key, files, description = row.key, dict(row.files), row.description or ""
        row.status, row.reviewed_by, row.review_note = "promoted", reviewer, note or None

    workspace = _materialize(key, files, settings)
    _register(key, workspace, description)
    log.info("mpp.promoted", proposal=proposal_id, key=key, workspace=workspace)
    return {"status": "promoted", "workspace": workspace}


def _materialize(key: str, files: dict[str, str], settings: Settings) -> str:
    """Write the promoted module under the Terraform workspaces dir (its own workspace)."""
    workspace = f"promoted-{key.replace('.', '-')}"
    root = Path(settings.terraform_workspaces_dir) / workspace
    root.mkdir(parents=True, exist_ok=True)
    for fname, content in files.items():
        (root / fname).write_text(content, encoding="utf-8")
    return workspace


def _register(key: str, workspace: str, description: str) -> None:
    cloud, resource = key.split(".", 1)
    templates.register_promoted(templates.WorkflowTemplate(
        key, cloud, resource, "v1-promoted", workspace, PromotedModuleInputs,
        description or f"Promoted module {key}", _todo_policy))


async def rehydrate_promoted(settings: Settings | None = None) -> int:
    """Startup: re-register every promoted module from the DB (the registry is in-memory)."""
    from sqlalchemy import select

    settings = settings or get_settings()
    count = 0
    async with session_scope() as s:
        rows = (await s.execute(select(ModuleProposal)
                                .where(ModuleProposal.status == "promoted"))).scalars().all()
        for row in rows:
            _register(row.key, _materialize(row.key, dict(row.files), settings),
                      row.description or "")
            count += 1
    if count:
        log.info("mpp.rehydrated", promoted=count)
    return count
