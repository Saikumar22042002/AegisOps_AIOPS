"""Real Terraform runner — shells to the `terraform` CLI.

init / validate / plan(-out then show -json) / apply / destroy / output. Plan JSON is parsed
into the PR-style diff + resource counts the artifact panel renders. State uses the local
backend by default (an S3+DynamoDB backend is configurable via env). Apply/destroy are only
invoked by the graph AFTER the human-approval interrupt — `-auto-approve` here applies the
already-approved, saved plan; it does not bypass the gate.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import structlog

from ..integrations.langfuse_client import get_tracer
from ..security.redaction import redact_dict
from ..settings import Settings
from .console import CommandConsole, LineCallback

log = structlog.get_logger(__name__)


class TerraformError(Exception):
    pass


def _var_args(variables: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for k, v in variables.items():
        val = v if isinstance(v, str) else json.dumps(v)
        args += ["-var", f"{k}={val}"]
    return args


def state_slug(name: str) -> str:
    """Stable, filesystem/Terraform-safe state-workspace slug for a resource name."""
    import re as _re
    slug = _re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-") or "unnamed"
    return ("res-" + slug)[:60]


def remove_run_plan_files(settings: Settings, run_id: str) -> int:
    """PR-1a: delete the run's .tfplan file(s) at terminal states — the reviewable record
    lives on in runs.plan_json. Plan files are named aegisops-<ws>-<run_id>.tfplan, so a
    run_id glob across all module dirs needs no workspace knowledge. Returns the count."""
    import glob as _glob

    if not run_id:
        return 0
    removed = 0
    pattern = os.path.join(settings.terraform_workspaces_dir, "*", f"aegisops-*-{run_id}.tfplan")
    for path in _glob.glob(pattern):
        try:
            os.remove(path)
            removed += 1
        except OSError as e:
            log.warning("terraform.plan_cleanup_failed", path=path, error=str(e))
    if removed:
        log.info("terraform.plan_files_removed", run_id=run_id, count=removed)
    return removed


def sweep_stray_plan_files(settings: Settings, max_age_days: int = 7,
                           keep_run_ids: set[str] | None = None) -> int:
    """PR-1a (sweeper): remove plan files older than the threshold — strays from crashed
    runs whose terminal cleanup never fired. BINDING: a NON-TERMINAL run's plan is never
    stray (awaiting_approval may legitimately wait days) — callers pass those run ids in
    `keep_run_ids` and any filename carrying one is skipped. Logged, never silent."""
    import glob as _glob
    import time as _time

    keep = keep_run_ids or set()
    cutoff = _time.time() - max_age_days * 86400
    removed = 0
    pattern = os.path.join(settings.terraform_workspaces_dir, "*", "*.tfplan")
    for path in _glob.glob(pattern):
        name = os.path.basename(path)
        if any(rid and rid in name for rid in keep):
            continue                       # a live/awaiting run's plan is NOT stray
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
                log.info("terraform.stray_plan_removed", path=name)
        except OSError as e:
            log.warning("terraform.stray_plan_sweep_failed", path=path, error=str(e))
    return removed


def prune_destroyed_state_workspace(settings: Settings, workspace: str,
                                    state_workspace: str) -> bool:
    """PR-1b (sweeper-only — NEVER inline at destroy, no chat request triggers this):
    prune a per-resource state dir whose terraform state holds ZERO resources. The state
    FILE is read directly (it IS the state; no subprocess). Returns True when pruned."""
    import shutil as _shutil

    if not state_workspace:
        return False
    ws_dir = os.path.join(settings.terraform_workspaces_dir, workspace,
                          "terraform.tfstate.d", state_workspace)
    state_file = os.path.join(ws_dir, "terraform.tfstate")
    if not os.path.isdir(ws_dir):
        return False
    try:
        if os.path.exists(state_file):
            with open(state_file, encoding="utf-8") as f:
                if json.load(f).get("resources"):
                    log.warning("terraform.prune_refused_nonempty_state",
                                workspace=workspace, state_workspace=state_workspace)
                    return False
        _shutil.rmtree(ws_dir)
        log.info("terraform.state_workspace_pruned", workspace=workspace,
                 state_workspace=state_workspace)
        return True
    except (OSError, json.JSONDecodeError) as e:
        log.warning("terraform.prune_failed", workspace=workspace,
                    state_workspace=state_workspace, error=str(e))
        return False


class TerraformRunner:
    """Runs terraform in a module directory. `state_workspace` (Phase 8 / N-08) selects a
    PER-RESOURCE Terraform workspace via the TF_WORKSPACE env var, giving every provisioned
    resource its own state file (terraform.tfstate.d/<slug>/terraform.tfstate). Without it,
    every apply in a module shared ONE local state, so a second create reconciled the same
    resource addresses and destroyed/replaced the previous resource — the confirmed
    "create deleted my instance" defect. Env-var selection (not `workspace select`) keeps
    concurrent runs race-free: nothing mutates .terraform/environment.
    None ⇒ the default workspace (legacy resources created before isolation)."""

    def __init__(self, workspace: str, settings: Settings, state_workspace: str | None = None,
                 run_id: str | None = None) -> None:
        self.bin = settings.terraform_bin
        self.workspace = workspace
        self.workdir = os.path.join(settings.terraform_workspaces_dir, workspace)
        self.settings = settings
        self.state_workspace = state_workspace or None
        self.run_id = run_id
        # A3: a UNIQUE plan-file per run — no two operations ever share a plan file, even two
        # creates of the same resource or two concurrent runs in the same module dir (the legacy
        # shared `aegisops.tfplan` could be overwritten by a racing plan). Falls back to the
        # per-resource / legacy name only when no run_id is threaded (e.g. reveal, which reads
        # outputs and never touches a plan file).
        if run_id:
            self.plan_file = f"aegisops-{self.state_workspace or 'default'}-{run_id}.tfplan"
        elif self.state_workspace:
            self.plan_file = f"aegisops-{self.state_workspace}.tfplan"
        else:
            self.plan_file = "aegisops.tfplan"

    def _env(self, include_ws: bool = True) -> dict[str, str]:
        env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "TF_CLI_ARGS": "-no-color"}
        # LAT: a shared provider plugin cache so `terraform init` reuses downloaded providers
        # across modules/resources instead of re-fetching them every time.
        if self.settings.tf_plugin_cache_dir:
            env["TF_PLUGIN_CACHE_DIR"] = self.settings.tf_plugin_cache_dir
        if self.state_workspace and include_ws:
            env["TF_WORKSPACE"] = self.state_workspace
        s = self.settings
        if s.aws_access_key_id:
            env["AWS_ACCESS_KEY_ID"] = s.aws_access_key_id
            env["AWS_SECRET_ACCESS_KEY"] = s.aws_secret_access_key
            env["AWS_DEFAULT_REGION"] = s.aws_default_region
            if s.aws_session_token:
                env["AWS_SESSION_TOKEN"] = s.aws_session_token
        # Azure — the azurerm provider authenticates via ARM_* (service principal).
        if s.azure_client_id:
            env["ARM_CLIENT_ID"] = s.azure_client_id
            env["ARM_CLIENT_SECRET"] = s.azure_client_secret
            env["ARM_TENANT_ID"] = s.azure_tenant_id
            env["ARM_SUBSCRIPTION_ID"] = s.azure_subscription_id
        # GCP — the google provider authenticates via a service-account key file + project.
        if s.google_cloud_project:
            env["GOOGLE_PROJECT"] = s.google_cloud_project
        if s.google_application_credentials:
            env["GOOGLE_APPLICATION_CREDENTIALS"] = s.google_application_credentials
        return env

    def _console(self, include_ws: bool = True) -> CommandConsole:
        if not os.path.isdir(self.workdir):
            raise TerraformError(f"Terraform workspace not found: {self.workdir}")
        return CommandConsole(cwd=self.workdir, env=self._env(include_ws))

    def _raise_stage_failure(self, stage: str, res, timeout_s: int) -> None:
        """PR-2b: rc 124 = the stage exceeded its budget and the PROCESS GROUP was killed.
        Classified honestly — the state may hold a lock; the reconciler/orphan sweep
        reconciles, and docs/TF_FORCE_UNLOCK.md carries the manual guidance."""
        if res.returncode == 124:
            raise TerraformError(
                f"terraform {stage} exceeded {timeout_s // 60}m and was terminated "
                "(process group killed). The state may still hold a lock; the "
                "reconciler/orphan sweep will reconcile the run — manual guidance: "
                "docs/TF_FORCE_UNLOCK.md")
        raise TerraformError(f"terraform {stage} failed:\n" + "\n".join(res.stderr[-15:]))

    def _span(self, op: str, **extra: Any):
        """Langfuse tool span for one terraform operation — inputs are redacted; a raised
        TerraformError is recorded ON the span (level=ERROR) and still propagates."""
        return get_tracer(self.settings).tool(
            f"terraform.{op}",
            input={"workdir": self.workdir, "state_workspace": self.state_workspace,
                   **redact_dict(extra)})

    def _backend_config_args(self) -> list[str]:
        """A3: remote-backend config supplied at init via `-backend-config` (S3 + DynamoDB lock).
        Empty in local mode (dev default). Enabling `remote` also requires the module's backend
        block to be `s3` — a documented migration; the state key is namespaced per module +
        per-resource workspace so locking is scoped correctly."""
        s = self.settings
        if s.aegisops_tf_backend != "remote" or not s.tf_state_bucket:
            return []
        key = f"{s.tf_state_key_prefix}/{self.workspace}/{self.state_workspace or 'default'}.tfstate"
        args = [f"-backend-config=bucket={s.tf_state_bucket}",
                f"-backend-config=key={key}",
                f"-backend-config=region={s.tf_state_region}"]
        if s.tf_state_dynamodb_table:
            args.append(f"-backend-config=dynamodb_table={s.tf_state_dynamodb_table}")
        return args

    def _is_initialized(self) -> bool:
        """Warm-init check (LAT): providers + backend are already set up for this module."""
        return (os.path.isdir(os.path.join(self.workdir, ".terraform"))
                and os.path.isfile(os.path.join(self.workdir, ".terraform.lock.hcl")))

    async def init(self, on_line: LineCallback | None = None, force: bool = False) -> dict[str, Any]:
        # init runs WITHOUT the TF_WORKSPACE override: terraform refuses to init while the
        # selected workspace doesn't exist yet. The workspace is ensured right after, and every
        # subsequent command carries TF_WORKSPACE.
        async with self._span("init") as t:
            # LAT: skip the full re-init on a warm module (the ~19s dominant per-turn cost). Only
            # when .terraform/ + lockfile are present; a stale/mismatched setup is covered by the
            # `force` escape hatch (and AEGISOPS_TF_SKIP_INIT_WHEN_READY=false).
            if (not force and self.settings.aegisops_tf_skip_init_when_ready
                    and self._is_initialized()):
                try:
                    if self.state_workspace:
                        await self.ensure_state_workspace()
                    t.output = {"ok": True, "skipped_init": True}
                    return {"ok": True, "skipped_init": True}
                except TerraformError as e:
                    # The module CLAIMED initialized (.terraform/ + lockfile) but isn't actually
                    # usable — e.g. the shared provider cache was evicted, leaving dangling plugin
                    # links. Fall back to a full init rather than fail the run (risk-doc: "fall
                    # back to full init on any mismatch").
                    log.warning("terraform.warm_init_fell_back", workspace=self.workspace,
                                error=str(e)[:200])
            init_args = [self.bin, "init", "-input=false", "-upgrade", *self._backend_config_args()] \
                if force else [self.bin, "init", "-input=false", *self._backend_config_args()]
            res = await self._console(include_ws=False).run(
                init_args, on_line, timeout=self.settings.tf_plan_timeout_s)
            if res.returncode != 0:
                self._raise_stage_failure("init", res, self.settings.tf_plan_timeout_s)
            if self.state_workspace:
                await self.ensure_state_workspace()
            t.output = {"ok": True, "skipped_init": False}
        return {"ok": True, "skipped_init": False}

    async def ensure_state_workspace(self) -> None:
        """Create this runner's Terraform workspace if it doesn't exist yet (idempotent).

        Runs `terraform workspace new` WITHOUT TF_WORKSPACE in the env — terraform refuses
        workspace commands while the override variable is set. "already exists" is success.
        """
        if not self.state_workspace:
            return
        if not os.path.isdir(self.workdir):
            raise TerraformError(f"Terraform workspace not found: {self.workdir}")
        env = {**os.environ, **self._env()}
        env.pop("TF_WORKSPACE", None)
        proc = await asyncio.create_subprocess_exec(
            self.bin, "workspace", "new", self.state_workspace, cwd=self.workdir, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _out, err = await proc.communicate()
        if proc.returncode != 0 and b"already exists" not in err:
            raise TerraformError(f"could not create state workspace {self.state_workspace}:\n"
                                 + err.decode(errors="replace")[-800:])

    async def validate(self, on_line: LineCallback | None = None) -> dict[str, Any]:
        res = await self._console().run([self.bin, "validate", "-json"], on_line)
        parsed = _parse_last_json(res.stdout)
        return parsed or {"valid": res.returncode == 0}

    async def plan(self, variables: dict[str, Any] | None = None, destroy: bool = False,
                   on_line: LineCallback | None = None) -> dict[str, Any]:
        async with self._span("plan", variables=variables or {}, destroy=destroy) as t:
            args = [self.bin, "plan", "-input=false", f"-out={self.plan_file}"]
            if destroy:
                args.append("-destroy")
            args += _var_args(variables or {})
            res = await self._console().run(args, on_line,
                                             timeout=self.settings.tf_plan_timeout_s)
            if res.returncode not in (0, 2):  # 0 = no changes, 2 = changes present (with -detailed-exitcode); plain plan returns 0
                self._raise_stage_failure("plan", res, self.settings.tf_plan_timeout_s)
            plan = await self.show_plan()
            t.output = plan.get("summary")
        return plan

    async def _capture_json(self, args: list[str]) -> tuple[int, str, str]:
        """Run a JSON-emitting terraform command capturing stdout RAW (no line pump, no
        redaction). The streamed console applies `redact()` per line, which can corrupt a big
        plan JSON (VM plans embed key material that trips the maskers) — the corrupted blob
        then failed to parse and the plan summary silently became +0 ~0 -0 (Phase 7): approvers
        were shown "0 resources" for plans that actually added 8. Raw capture is safe here:
        the parsed JSON is reduced to addresses/actions, and `output()` strips sensitive
        values before anything is surfaced or persisted.
        """
        if not os.path.isdir(self.workdir):
            raise TerraformError(f"Terraform workspace not found: {self.workdir}")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=self.workdir, env={**os.environ, **self._env()},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def show_plan(self) -> dict[str, Any]:
        rc, out, err = await self._capture_json([self.bin, "show", "-json", self.plan_file])
        if rc != 0:
            raise TerraformError("terraform show failed:\n" + err[-2000:])
        try:
            data = json.loads(out.strip())
        except json.JSONDecodeError as e:
            # NEVER degrade to a fake +0 summary — an approver must not approve a plan card
            # that claims zero changes for a plan that isn't zero.
            raise TerraformError(f"could not parse the plan JSON ({e}); refusing to report a "
                                 "zero-change summary for an unparsed plan") from e
        # U1: stash the planned resource attributes (change.after) for REAL policy evaluation.
        # In-memory only — the reduced summary/diff is what gets persisted to runs.plan_json, so
        # no raw `after` (which could carry a sensitive attribute) is ever persisted.
        self._planned_resources = [
            {"type": rc_.get("type"), "name": rc_.get("name"), "address": rc_.get("address"),
             "after": (rc_.get("change", {}) or {}).get("after") or {}}
            for rc_ in data.get("resource_changes", [])
        ]
        return _summarize_plan(data)

    def planned_resources(self) -> list[dict[str, Any]]:
        """The planned resources' `after` attributes (from the last show_plan) — for U1 policy
        evaluation against the real plan. Empty until show_plan has run."""
        return getattr(self, "_planned_resources", [])

    async def output_raw(self, name: str) -> str:
        """A single output's raw value — used ONLY by the one-time credential reveal (N-02).
        Raw capture: the value must not pass through the redacting console (it would be
        masked) nor be logged anywhere. The API layer enforces the one-shot semantics."""
        rc, out, err = await self._capture_json([self.bin, "output", "-raw", name])
        if rc != 0:
            raise TerraformError(f"terraform output -raw {name} failed:\n" + err[-400:])
        return out

    async def state_list(self) -> list[str]:
        """Resource addresses currently in this workspace's state (read-only; for honest
        partial-failure reporting)."""
        rc, out, _err = await self._capture_json([self.bin, "state", "list"])
        if rc != 0:
            return []  # no state file yet ⇒ nothing applied
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def apply(self, on_line: LineCallback | None = None) -> dict[str, Any]:
        """Apply the previously-saved, human-approved plan."""
        async with self._span("apply") as t:
            res = await self._console().run(
                [self.bin, "apply", "-input=false", "-auto-approve", self.plan_file], on_line,
                timeout=self.settings.tf_apply_timeout_s,
            )
            if res.returncode != 0:
                self._raise_stage_failure("apply", res, self.settings.tf_apply_timeout_s)
            result = {"applied": True, **(await self.output())}
            t.output = {"applied": True, "outputs": sorted(result.get("outputs", {}).keys()),
                        "sensitive_outputs": result.get("sensitive_outputs", [])}
        return result

    async def destroy(self, variables: dict[str, Any] | None = None,
                      on_line: LineCallback | None = None) -> dict[str, Any]:
        async with self._span("destroy", variables=variables or {}) as t:
            await self.plan(variables, destroy=True, on_line=on_line)
            res = await self._console().run(
                [self.bin, "apply", "-input=false", "-auto-approve", self.plan_file], on_line,
                timeout=self.settings.tf_apply_timeout_s,
            )
            if res.returncode != 0:
                self._raise_stage_failure("destroy", res, self.settings.tf_apply_timeout_s)
            t.output = {"destroyed": True}
        return {"destroyed": True}

    async def output(self) -> dict[str, Any]:
        rc, out, _err = await self._capture_json([self.bin, "output", "-json"])
        if rc != 0:
            return {"outputs": {}}
        try:
            data = json.loads(out.strip()) or {}
        except json.JSONDecodeError:
            log.warning("terraform.output_parse_failed")
            return {"outputs": {}}
        # Exclude sensitive outputs (e.g. private_key_pem) — never surface/persist secret material.
        # The operator retrieves them out-of-band via `terraform output -raw <name>`.
        return {"outputs": {k: v.get("value") for k, v in data.items() if not v.get("sensitive")},
                "sensitive_outputs": [k for k, v in data.items() if v.get("sensitive")]}


def _parse_last_json(lines: list[str]) -> dict | None:
    """Terraform JSON commands print a single JSON document across the captured lines."""
    blob = "\n".join(lines).strip()
    if not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        # validate/plan may interleave; try the last complete JSON object.
        for line in reversed(lines):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return None


def _summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    add = change = destroy = 0
    diff: list[dict[str, Any]] = []
    for rc in plan.get("resource_changes", []):
        actions = rc.get("change", {}).get("actions", [])
        if actions == ["create"]:
            add += 1
            sign = "+"
        elif actions == ["delete"]:
            destroy += 1
            sign = "-"
        elif "update" in actions or set(actions) == {"create", "delete"}:
            change += 1
            sign = "~"
        else:
            sign = " "
        diff.append({"address": rc.get("address"), "type": rc.get("type"), "actions": actions, "sign": sign})
    return {"summary": {"add": add, "change": change, "destroy": destroy}, "diff": diff,
            "format_version": plan.get("format_version")}


def get_terraform(workspace: str, settings: Settings) -> TerraformRunner:
    return TerraformRunner(workspace, settings)
