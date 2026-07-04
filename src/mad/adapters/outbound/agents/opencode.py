from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from mad.adapters.outbound.agents._subprocess import (
    _STDOUT_BUFFER_LIMIT,
    _iter_stdout_lines,
    _scrub,
    _subprocess_env,
)
from mad.adapters.outbound.agents.hook_socket import resolve_hook_socket_path
from mad.core.config.settings import load_settings
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
from mad.core.orchestration.domain.timeout_config import DEFAULT_AGENT_TIMEOUT_S

# OpenCode does not expose structured retry events; fall back to stderr
# pattern matching to detect rate-limit exits.
_RATE_LIMIT_STDERR_PATTERNS = (
    "rate_limit",
    "rate limit",
    "429",
    "overloaded",
    "529",
    "session limit",
    "resets ",
    "temporarily limiting",
    "at capacity",
    "too many requests",
)


class OpenCodeProvider:
    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None:
        executable = load_settings().opencode_bin.value or shutil.which("opencode")
        if not executable:
            await emit(
                "session.error",
                {"type": "session.error", "error": "opencode CLI binary not found"},
            )
            return None

        # The use case resolves the effective timeout (per-session override >
        # MAD_AGENT_TIMEOUT_S env > 600 s) and passes it as ``timeout_s``; the
        # launcher no longer reads any timeout env var directly (issue #61).
        timeout = timeout_s if timeout_s is not None else DEFAULT_AGENT_TIMEOUT_S

        env = _subprocess_env()
        env["MAD_SESSION_ID"] = session_id
        env["MAD_HOOK_SOCKET"] = resolve_hook_socket_path()
        env["MAD_PROVIDER"] = "opencode"

        args = [executable, "run", "--format", "json"]
        if conversation_id is not None:
            args += ["--session", conversation_id]
        if model is not None:
            args += ["--model", model]
        if effort is not None:
            args += ["--variant", effort]
        args.append(prompt)

        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            limit=_STDOUT_BUFFER_LIMIT,
        )

        captured_id: str | None = None
        conversation_started_emitted = False

        try:
            async with asyncio.timeout(timeout):
                async for line in _iter_stdout_lines(proc.stdout):
                    await emit("agent.output", {"type": "agent.output", "line": line})
                    # Parse each JSON line; sessionID is present on every event.
                    # Emit agent.conversation_started the first time we see it.
                    if not conversation_started_emitted:
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        sid = obj.get("sessionID")
                        if sid and isinstance(sid, str):
                            captured_id = sid
                            conversation_started_emitted = True
                            await emit(
                                "agent.conversation_started",
                                {
                                    "conversation_id": sid,
                                    "provider": "opencode",
                                },
                            )
                await proc.wait()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            await emit(
                "session.error",
                {"type": "session.error", "error": f"timed out after {timeout}s"},
            )
            return captured_id
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            await emit("session.error", {"type": "session.error", "error": "cancelled"})
            raise

        if proc.returncode == 0:
            await emit(
                "session.status_idle",
                {"type": "session.status_idle", "stop_reason": "end_turn"},
            )
            return captured_id

        # Non-zero exit: check stderr for rate-limit patterns.
        stderr_raw = b""
        if proc.stderr:
            stderr_raw = await proc.stderr.read()
        stderr_text = stderr_raw.decode(errors="replace")
        stderr_tail = stderr_text[-2000:]

        lower = stderr_tail.lower()
        if any(pat in lower for pat in _RATE_LIMIT_STDERR_PATTERNS):
            # Do NOT emit session.error — dispatcher retries.
            raise RateLimitError(captured_id=captured_id, reason="rate_limit")

        scrubbed = _scrub(stderr_tail)
        await emit(
            "session.error",
            {
                "type": "session.error",
                "error": scrubbed,
                "exit_code": proc.returncode,
            },
        )
        return captured_id
