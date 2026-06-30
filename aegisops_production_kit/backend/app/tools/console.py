"""Streaming command console.

Runs a command as an async subprocess, streaming stdout/stderr line-by-line (secret-masked)
to a callback for SSE, and supports stdin injection for interactive prompts (password/input)
which arrive over REST. The executor interface allows a Docker-exec / K8s-Job sandbox backend
later; the default runs in the API image (which has terraform, ansible, and kubectl on PATH).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import structlog

from ..security.redaction import redact

log = structlog.get_logger(__name__)

LineCallback = Callable[[str, str], Awaitable[None]]  # (stream, line)


@dataclass
class CommandResult:
    returncode: int
    stdout: list[str] = field(default_factory=list)
    stderr: list[str] = field(default_factory=list)


class CommandConsole:
    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None, timeout: float = 1800.0) -> None:
        self.cwd = cwd
        # Inherit PATH (terraform/ansible/kubectl) but only overlay explicit env.
        self.env = {**os.environ, **(env or {})}
        self.timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None

    async def run(self, args: list[str], on_line: LineCallback | None = None,
                  stdin_data: str | None = None) -> CommandResult:
        log.info("console.exec", argv=args[0], args_count=len(args), cwd=self.cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # `terraform show -json` can emit a single line far larger than asyncio's default
            # 64 KB readline limit (big modules) — raise it to 32 MB.
            limit=32 * 1024 * 1024,
        )
        if stdin_data and self._proc.stdin:
            self._proc.stdin.write(stdin_data.encode())
            await self._proc.stdin.drain()
            self._proc.stdin.close()

        result = CommandResult(returncode=-1)

        async def _pump(stream: asyncio.StreamReader, name: str, sink: list[str]) -> None:
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = redact(raw.decode(errors="replace").rstrip("\n"))
                sink.append(line)
                if on_line:
                    await on_line(name, line)

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(self._proc.stdout, "stdout", result.stdout),
                    _pump(self._proc.stderr, "stderr", result.stderr),
                ),
                timeout=self.timeout,
            )
            result.returncode = await asyncio.wait_for(self._proc.wait(), timeout=30)
        except TimeoutError:
            self._proc.kill()
            result.returncode = 124
            if on_line:
                await on_line("stderr", f"command timed out after {self.timeout}s")
        log.info("console.exec_done", argv=args[0], rc=result.returncode)
        return result

    async def send_input(self, data: str) -> None:
        """Inject data into the running process's stdin (interactive prompt answer)."""
        if self._proc and self._proc.stdin and not self._proc.stdin.is_closing():
            self._proc.stdin.write((data + "\n").encode())
            await self._proc.stdin.drain()
