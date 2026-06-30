"""Real Ansible runner — shells to `ansible-playbook`, streams output, parses results."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import structlog

from ..settings import Settings
from .console import CommandConsole, LineCallback

log = structlog.get_logger(__name__)

_RECAP = re.compile(r"ok=(\d+).*changed=(\d+).*unreachable=(\d+).*failed=(\d+)")


class AnsibleError(Exception):
    pass


class AnsibleRunner:
    def __init__(self, settings: Settings) -> None:
        self.bin = settings.ansible_bin
        self.settings = settings

    async def run_playbook(self, playbook: str, inventory: str | None = None,
                           extra_vars: dict[str, Any] | None = None,
                           on_line: LineCallback | None = None) -> dict[str, Any]:
        if not os.path.exists(playbook):
            raise AnsibleError(f"Playbook not found: {playbook}")
        args = [self.bin, playbook]
        if inventory:
            args += ["-i", inventory]
        if extra_vars:
            args += ["-e", json.dumps(extra_vars)]
        console = CommandConsole(env={"ANSIBLE_FORCE_COLOR": "0"})
        res = await console.run(args, on_line)
        recap = _parse_recap(res.stdout)
        if res.returncode != 0:
            raise AnsibleError(f"ansible-playbook failed (rc={res.returncode}); recap={recap}")
        return {"ok": True, "recap": recap}

    async def version(self) -> str:
        res = await CommandConsole().run([self.bin, "--version"])
        return res.stdout[0] if res.stdout else ""


def _parse_recap(lines: list[str]) -> dict[str, int]:
    for line in reversed(lines):
        m = _RECAP.search(line)
        if m:
            return {"ok": int(m.group(1)), "changed": int(m.group(2)),
                    "unreachable": int(m.group(3)), "failed": int(m.group(4))}
    return {}


def get_ansible(settings: Settings) -> AnsibleRunner:
    return AnsibleRunner(settings)
