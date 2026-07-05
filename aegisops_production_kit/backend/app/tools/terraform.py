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


class TerraformRunner:
    def __init__(self, workspace: str, settings: Settings) -> None:
        self.bin = settings.terraform_bin
        self.workdir = os.path.join(settings.terraform_workspaces_dir, workspace)
        self.settings = settings
        self.plan_file = "aegisops.tfplan"

    def _env(self) -> dict[str, str]:
        env = {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "TF_CLI_ARGS": "-no-color"}
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

    def _console(self) -> CommandConsole:
        if not os.path.isdir(self.workdir):
            raise TerraformError(f"Terraform workspace not found: {self.workdir}")
        return CommandConsole(cwd=self.workdir, env=self._env())

    async def init(self, on_line: LineCallback | None = None) -> dict[str, Any]:
        res = await self._console().run([self.bin, "init", "-input=false"], on_line)
        if res.returncode != 0:
            raise TerraformError("terraform init failed:\n" + "\n".join(res.stderr[-15:]))
        return {"ok": True}

    async def validate(self, on_line: LineCallback | None = None) -> dict[str, Any]:
        res = await self._console().run([self.bin, "validate", "-json"], on_line)
        parsed = _parse_last_json(res.stdout)
        return parsed or {"valid": res.returncode == 0}

    async def plan(self, variables: dict[str, Any] | None = None, destroy: bool = False,
                   on_line: LineCallback | None = None) -> dict[str, Any]:
        args = [self.bin, "plan", "-input=false", f"-out={self.plan_file}"]
        if destroy:
            args.append("-destroy")
        args += _var_args(variables or {})
        res = await self._console().run(args, on_line)
        if res.returncode not in (0, 2):  # 0 = no changes, 2 = changes present (with -detailed-exitcode); plain plan returns 0
            raise TerraformError("terraform plan failed:\n" + "\n".join(res.stderr[-15:]))
        return await self.show_plan()

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
        return _summarize_plan(data)

    async def state_list(self) -> list[str]:
        """Resource addresses currently in this workspace's state (read-only; for honest
        partial-failure reporting)."""
        rc, out, _err = await self._capture_json([self.bin, "state", "list"])
        if rc != 0:
            return []  # no state file yet ⇒ nothing applied
        return [line.strip() for line in out.splitlines() if line.strip()]

    async def apply(self, on_line: LineCallback | None = None) -> dict[str, Any]:
        """Apply the previously-saved, human-approved plan."""
        res = await self._console().run(
            [self.bin, "apply", "-input=false", "-auto-approve", self.plan_file], on_line
        )
        if res.returncode != 0:
            raise TerraformError("terraform apply failed:\n" + "\n".join(res.stderr[-15:]))
        return {"applied": True, **(await self.output())}

    async def destroy(self, variables: dict[str, Any] | None = None,
                      on_line: LineCallback | None = None) -> dict[str, Any]:
        await self.plan(variables, destroy=True, on_line=on_line)
        res = await self._console().run(
            [self.bin, "apply", "-input=false", "-auto-approve", self.plan_file], on_line
        )
        if res.returncode != 0:
            raise TerraformError("terraform destroy failed:\n" + "\n".join(res.stderr[-15:]))
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
