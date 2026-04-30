from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Callable, Coroutine


class ClaudeCLIError(Exception):
    def __init__(self, exit_code: int, stderr_tail: str) -> None:
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"claude CLI exited {exit_code}: {stderr_tail}")


def _scrub(text: str) -> str:
    text = re.sub(r'sk-ant-[A-Za-z0-9_-]+', '[REDACTED]', text)
    text = re.sub(r'(?i)(token|key|secret|password)[=:\s]+\S+', r'\1=[REDACTED]', text)
    return text


def _subprocess_env() -> dict[str, str]:
    """Build an env dict for the subprocess.

    Ensures PATH includes the directory of the current Python interpreter so
    that shebang lines (#!/usr/bin/env python3) work even when the calling
    process has a restricted PATH (e.g. during tests).
    """
    env = dict(os.environ)
    python_dir = str(Path(sys.executable).parent)
    current_path = env.get("PATH", "")
    path_entries = current_path.split(os.pathsep) if current_path else []
    standard = ["/usr/local/bin", "/usr/bin", "/bin", python_dir]
    for entry in standard:
        if entry not in path_entries:
            path_entries.append(entry)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


class ClaudeCLIProvider:
    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None:
        executable = os.environ.get("MAD_CLAUDE_CLI_BIN") or shutil.which("claude")
        if not executable:
            await emit("session.error", {"type": "session.error", "error": "claude CLI binary not found"})
            return

        timeout = float(os.environ.get("MAD_CLAUDE_CLI_TIMEOUT_S", "600"))

        proc = await asyncio.create_subprocess_exec(
            executable,
            "--dangerously-skip-permissions",
            "-p", prompt,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )

        try:
            async with asyncio.timeout(timeout):
                async for line_bytes in proc.stdout:
                    line = line_bytes.decode(errors="replace").rstrip("\n")
                    await emit("agent.output", {"type": "agent.output", "line": line})
                await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            await emit("session.error", {"type": "session.error", "error": f"timed out after {timeout}s"})
            return
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            await emit("session.error", {"type": "session.error", "error": "cancelled"})
            raise

        if proc.returncode == 0:
            await emit("session.status_idle", {"type": "session.status_idle", "stop_reason": "end_turn"})
        else:
            stderr_raw = b""
            if proc.stderr:
                stderr_raw = await proc.stderr.read()
            stderr_text = stderr_raw.decode(errors="replace")
            scrubbed = _scrub(stderr_text[-2000:])
            await emit("session.error", {"type": "session.error", "error": scrubbed, "exit_code": proc.returncode})
