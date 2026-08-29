"""Streaming command console.

Runs a command as an async subprocess, streaming stdout/stderr line-by-line (secret-masked)
to a callback for SSE. Commands are run non-interactively (terraform `-auto-approve`, ansible
without prompts) — the human-in-the-loop is the approval gate, not stdin. The executor interface
allows a Docker-exec / K8s-Job sandbox backend later; the default runs in the API image (which
has terraform, ansible, and kubectl on PATH).
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
                  timeout: float | None = None) -> CommandResult:
        """PR-2b: `timeout` overrides the console default per call (terraform stages carry
        their own budgets). Expiry kills the whole PROCESS GROUP — SIGTERM, a short grace,
        then SIGKILL — so provider child processes never linger; rc becomes 124."""
        effective_timeout = timeout if timeout is not None else self.timeout
        log.info("console.exec", argv=args[0], args_count=len(args), cwd=self.cwd,
                 timeout_s=effective_timeout)
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=self.cwd,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,  # non-interactive: no stdin prompts are answered
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # `terraform show -json` can emit a single line far larger than asyncio's default
            # 64 KB readline limit (big modules) — raise it to 32 MB.
            limit=32 * 1024 * 1024,
            # PR-2b: its own session/process group, so a timeout can kill terraform AND the
            # provider plugin children it spawned.
            start_new_session=True,
        )

        result = CommandResult(returncode=-1)

        callback_dead = False

        async def _pump(stream: asyncio.StreamReader, name: str, sink: list[str]) -> None:
            # Prod-hardening (2026-08-17): the on_line callback is TRANSPORT (SSE/Redis
            # mirroring) — its failure must never stop reading the subprocess. An unread
            # pipe would fill and hang a live terraform apply mid-mutation; the durable
            # record (sink → result) is what matters. First callback failure logs; the
            # callback is then dropped for the rest of this command.
            nonlocal callback_dead
            while True:
                raw = await stream.readline()
                if not raw:
                    break
                line = redact(raw.decode(errors="replace").rstrip("\n"))
                sink.append(line)
                if on_line and not callback_dead:
                    try:
                        await on_line(name, line)
                    except Exception as cb_exc:  # noqa: BLE001 — transport loss ≠ command failure
                        callback_dead = True
                        log.warning("console.on_line_callback_failed_dropping",
                                    error=str(cb_exc)[:200])

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _pump(self._proc.stdout, "stdout", result.stdout),
                    _pump(self._proc.stderr, "stderr", result.stderr),
                ),
                timeout=effective_timeout,
            )
            result.returncode = await asyncio.wait_for(self._proc.wait(), timeout=30)
        except TimeoutError:
            await self._kill_group()
            result.returncode = 124
            # The honest timeout signal is recorded on the result itself (not only via the
            # optional callback), so a caller without an on_line still sees why rc=124.
            timeout_msg = f"command timed out after {effective_timeout}s"
            result.stderr.append(timeout_msg)
            if on_line:
                await on_line("stderr", timeout_msg)
        log.info("console.exec_done", argv=args[0], rc=result.returncode)
        return result

    async def _kill_group(self) -> None:
        """SIGTERM → grace → SIGKILL on the whole process group (PR-2b binding)."""
        import signal

        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            pgid = os.getpgid(self._proc.pid)
            os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=10)
                return
            except TimeoutError:
                os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            self._proc.kill()      # group gone or unsupported — fall back to the child
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=10)
        except TimeoutError:
            log.error("console.kill_group_unreaped", pid=self._proc.pid)
