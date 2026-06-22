from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from collections import deque
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
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
from mad.core.orchestration.domain.timeout_config import DEFAULT_AGENT_TIMEOUT_S

# Errors the CLI reports (in system/api_retry events, in the top-level
# ``error`` of an assistant message, or anywhere else in the stream) that
# are transient and retriable.  ``billing_error`` stays absent — a real
# auth/billing failure is terminal and must drain to session.error.
# ``authentication_failed`` IS retriable: a single-request ``401`` is almost
# always a transient blip (issue #72) — the same credentials succeed seconds
# later — so it rides the existing #62 backoff + conversation-resume path
# instead of draining the queue.  A genuine credential problem surfaces as a
# persistent failure and is exhausted by the dispatcher's cumulative ceiling.
_RETRIABLE_ERRORS = frozenset({"rate_limit", "overloaded", "authentication_failed"})

# HTTP statuses the CLI reports on a terminal ``result`` event that mean
# "transient upstream signal to back off and retry" rather than a real
# failure: 429 (usage/session limit), 529 (overloaded), and 401 (transient
# authentication_failed — issue #72).
_RETRIABLE_HTTP_STATUS = frozenset({429, 529, 401})

# How many trailing stdout lines to keep for the text fallback.  The CLI
# prints its terminal rate-limit shape (rate_limit_event / assistant /
# result) in the last handful of lines, so a small bounded tail is enough
# and keeps memory flat regardless of total output volume.
_STDOUT_TAIL_LINES = 30

# Stderr substrings that confirm a rate-limit exit when structured events
# are unavailable (e.g. the process exits before emitting api_retry).
_RATE_LIMIT_STDERR_PATTERNS = (
    "rate_limit",
    "429",
    "overloaded",
    "529",
    "session limit",
    "resets ",
    "temporarily limiting",
    "at capacity",
)


def _coerce_epoch(value: Any) -> float | None:
    """Return ``value`` as a Unix-epoch float, or ``None`` if not numeric.

    ``resetsAt`` arrives as an integer epoch (seconds) but is defended
    against malformed/missing values so a bad payload never crashes the
    launcher — it just falls back to the plain backoff schedule.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ClaudeCLIError(Exception):
    def __init__(self, exit_code: int, stderr_tail: str) -> None:
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"claude CLI exited {exit_code}: {stderr_tail}")


class ClaudeCLIProvider:
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
        executable = os.environ.get("MAD_CLAUDE_CLI_BIN") or shutil.which("claude")
        if not executable:
            await emit(
                "session.error",
                {"type": "session.error", "error": "claude CLI binary not found"},
            )
            return None

        # The use case resolves the effective timeout (per-session override >
        # MAD_AGENT_TIMEOUT_S env > 600 s) and passes it as ``timeout_s``; the
        # launcher no longer reads any timeout env var directly (issue #61).
        timeout = timeout_s if timeout_s is not None else DEFAULT_AGENT_TIMEOUT_S

        env = _subprocess_env()
        env["MAD_SESSION_ID"] = session_id
        env["MAD_HOOK_SOCKET"] = resolve_hook_socket_path()
        env["MAD_PROVIDER"] = "claude_cli"
        # Disable the CLI's own retry loop so Mad owns the full retry
        # schedule and can emit task.retrying events with correct backoff.
        env["CLAUDE_CODE_MAX_RETRIES"] = "0"

        args = [
            executable,
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
            "--verbose",
            "-p",
            prompt,
        ]
        if conversation_id is not None:
            args += ["--resume", conversation_id]
        if model is not None:
            args += ["--model", model]
        if effort is not None:
            args += ["--effort", effort]

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
        rate_limit_detected = False
        rate_limit_reason = "rate_limit"
        resets_at: float | None = None
        recent_lines: deque[str] = deque(maxlen=_STDOUT_TAIL_LINES)
        # Diagnostic detail carried out of the stream so a terminal
        # session.error is self-describing (issue #72): the actual error
        # string / HTTP status / request_id otherwise live only in the raw
        # agent.output log.  Populated from the last assistant error and the
        # terminal result; emptied keys are dropped before emitting.
        last_error: str | None = None
        last_api_error_status: int | None = None
        last_request_id: str | None = None

        try:
            async with asyncio.timeout(timeout):
                async for line in _iter_stdout_lines(proc.stdout):
                    await emit("agent.output", {"type": "agent.output", "line": line})
                    recent_lines.append(line)
                    # Parse every JSON line. The stream-json format carries
                    # session_id on system/init (first line), api_retry, and
                    # result. Parsing every line lets us detect rate-limit
                    # signals from api_retry events even after conversation
                    # start is already recorded.
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    # Update conversation ID from any event that carries it.
                    sid = obj.get("session_id")
                    if sid and isinstance(sid, str) and captured_id is None:
                        captured_id = sid

                    # Emit agent.conversation_started once, as soon as we
                    # have an ID.
                    if not conversation_started_emitted and captured_id:
                        conversation_started_emitted = True
                        await emit(
                            "agent.conversation_started",
                            {
                                "conversation_id": captured_id,
                                "provider": "claude_cli",
                            },
                        )

                    # Detect the rate-limit signal across every shape the CLI
                    # emits.  With CLAUDE_CODE_MAX_RETRIES=0 the CLI does NOT
                    # emit system/api_retry — it surfaces a real usage/session
                    # limit as a terminal stdout result (is_error + 429) plus a
                    # rate_limit_event (status="rejected") and an assistant
                    # message carrying error="rate_limit".  All of these are
                    # parsed here so the limit is classified as retriable
                    # (issue #62 follow-up); a stderr-only check missed them
                    # because the CLI wrote nothing to stderr.
                    obj_type = obj.get("type")

                    if obj_type == "system" and obj.get("subtype") == "api_retry":
                        # Legacy path: only fires when the CLI runs its own
                        # internal retries (CLAUDE_CODE_MAX_RETRIES > 0).
                        error = obj.get("error", "rate_limit")
                        if error in _RETRIABLE_ERRORS:
                            rate_limit_detected = True
                            rate_limit_reason = error
                            # api_retry events include session_id — prefer
                            # this as the most recent capture point.
                            retry_sid = obj.get("session_id")
                            if retry_sid and isinstance(retry_sid, str):
                                captured_id = retry_sid

                    elif obj_type == "rate_limit_event":
                        # Usage/session-limit signal. status="rejected" means
                        # the request was refused and will reset at resetsAt.
                        # An overage rejection (org_level_disabled) is still a
                        # limit that resets — retriable, NOT a billing failure.
                        info = obj.get("rate_limit_info") or {}
                        if info.get("status") == "rejected":
                            rate_limit_detected = True
                            resets_at = _coerce_epoch(info.get("resetsAt"))

                    elif obj_type == "result" and obj.get("is_error"):
                        # Terminal result of a failed turn. A 429/529/401 status
                        # is the authoritative transient signal (issue #72 adds
                        # 401).  Capture the diagnostic detail so a terminal
                        # session.error is self-describing rather than empty.
                        status = obj.get("api_error_status")
                        if isinstance(status, int):
                            last_api_error_status = status
                        result_text = obj.get("result")
                        if isinstance(result_text, str) and result_text:
                            last_error = result_text
                        if status in _RETRIABLE_HTTP_STATUS:
                            rate_limit_detected = True
                            # A transient 401 resumes under the auth-failed
                            # reason; 429/529 keep the rate-limit reason already
                            # set (default or from an earlier assistant/api_retry).
                            if status == 401:
                                rate_limit_reason = "authentication_failed"

                    elif obj_type == "assistant":
                        # Synthetic assistant message the CLI emits when a turn
                        # is rejected carries a top-level error enum.
                        error = obj.get("error")
                        if isinstance(error, str) and error:
                            last_error = error
                        request_id = obj.get("request_id")
                        if isinstance(request_id, str) and request_id:
                            last_request_id = request_id
                        if error in _RETRIABLE_ERRORS:
                            rate_limit_detected = True
                            rate_limit_reason = error

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

        # Non-zero exit: read stderr for error detail and rate-limit fallback.
        stderr_raw = b""
        if proc.stderr:
            stderr_raw = await proc.stderr.read()
        stderr_text = stderr_raw.decode(errors="replace")
        stderr_tail = stderr_text[-2000:]

        # Text pattern fallback: detect rate-limit when the structured
        # parse above did not fire (e.g. an unrecognised stream shape or a
        # crash before the terminal events).  Scan BOTH stderr and the
        # trailing stdout lines — a real session limit prints its text to
        # stdout ("You've hit your session limit · resets …"), leaving
        # stderr empty, which a stderr-only scan missed.
        if not rate_limit_detected:
            haystack = "\n".join((*recent_lines, stderr_tail)).lower()
            if any(pat in haystack for pat in _RATE_LIMIT_STDERR_PATTERNS):
                rate_limit_detected = True

        if rate_limit_detected:
            # Do NOT emit session.error — the dispatcher will retry and
            # only emits task.failed after the ceiling is exhausted.
            retry_after_floor_s = (
                max(0.0, resets_at - time.time()) if resets_at is not None else None
            )
            raise RateLimitError(
                captured_id=captured_id,
                reason=rate_limit_reason,
                retry_after_floor_s=retry_after_floor_s,
            )

        # Prefer the structured error string from the stream (issue #72): a
        # terminal failure (e.g. a persistent 401/500) often leaves stderr
        # empty, so the scrubbed stderr would be "" — emitting the captured
        # ``result``/assistant error keeps session.error diagnosable.  Fall
        # back to scrubbed stderr when no structured error was seen.
        scrubbed = _scrub(stderr_tail)
        error_detail = scrubbed or (_scrub(last_error) if last_error else "")
        error_data: dict[str, Any] = {
            "type": "session.error",
            "error": error_detail,
            "exit_code": proc.returncode,
        }
        if last_api_error_status is not None:
            error_data["api_error_status"] = last_api_error_status
        if last_request_id is not None:
            error_data["request_id"] = last_request_id
        await emit("session.error", error_data)
        return captured_id
