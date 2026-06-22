"""Unit tests for the ClaudeCLI provider — mapped 1:1 to specs/claude-cli/requirements.md.

AC → test mapping:
  AC-1  stdout lines → agent.output events                  → test_claude_cli_ac1_*
  AC-2  exit 0 → session.status_idle                       → test_claude_cli_ac2_*
  AC-3  exit 1 + token in stderr → session.error/redacted  → test_claude_cli_ac3_*
  AC-4  timeout → session.error < 2s, no zombie            → test_claude_cli_ac4_*
  AC-5  MAD_CLAUDE_CLI_BIN custom path is invoked           → test_claude_cli_ac5_*
  AC-10 provider-level rate-limit detection                 → test_claude_cli_rate_limit_*

These tests are EXPECTED to fail (red) until ClaudeCLIProvider is implemented.
Tests use fake binary scripts in tmp_path — no real `claude` binary is invoked.
"""

from __future__ import annotations

import stat
import textwrap
import time
from pathlib import Path

import pytest

from mad.adapters.outbound.agents.claude_cli import ClaudeCLIProvider
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executable_script(path: Path, source: str) -> Path:
    """Write a Python shebang script, make it executable, and return its path."""
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(source))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


async def _collect_emit(
    launcher: ClaudeCLIProvider,
    prompt: str,
    workspace: Path,
    session_id: str = "test-session-id",
) -> list[dict]:
    """Run the launcher and collect all emitted events into a list."""
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(session_id=session_id, prompt=prompt, workspace=workspace, emit=capture)
    return collected


# ---------------------------------------------------------------------------
# AC-1: stdout lines → 3 agent.output events with correct content
# Covers FR-1, FR-2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac1_stdout_lines_emitted_as_agent_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Three stdout lines from the fake binary must produce 3 agent.output events."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys
        print("first output line")
        print("second output line")
        print("third output line")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    events = await _collect_emit(launcher, prompt="test", workspace=tmp_path)

    output_events = [e for e in events if e.get("type") == "agent.output"]
    assert len(output_events) == 3, (
        f"Expected 3 agent.output events, got {len(output_events)}: {output_events}"
    )
    lines = [e["line"] for e in output_events]
    assert "first output line" in lines
    assert "second output line" in lines
    assert "third output line" in lines


# ---------------------------------------------------------------------------
# AC-2: exit 0 → session.status_idle with stop_reason="end_turn"
# Covers FR-3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac2_exit_zero_emits_status_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess exit code 0 must produce session.status_idle(stop_reason='end_turn')."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys
        print("done")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    events = await _collect_emit(launcher, prompt="test", workspace=tmp_path)

    idle_events = [e for e in events if e.get("type") == "session.status_idle"]
    assert len(idle_events) >= 1, (
        f"Expected session.status_idle, got event types: {[e.get('type') for e in events]}"
    )
    assert idle_events[-1].get("stop_reason") == "end_turn", (
        f"session.status_idle must carry stop_reason='end_turn', got: {idle_events[-1]}"
    )


# ---------------------------------------------------------------------------
# AC-3: exit 1 with token in stderr → session.error with token REDACTED
# Covers FR-4, FR-8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac3_nonzero_exit_emits_error_with_redacted_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subprocess exit code 1 with a token in stderr → session.error; token must be [REDACTED]."""
    secret_token = "sk-ant-api03-supersecret-token-value-ABCDEF12345"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        print("{secret_token}", file=sys.stderr)
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    events = await _collect_emit(launcher, prompt="test", workspace=tmp_path)

    error_events = [e for e in events if e.get("type") == "session.error"]
    assert len(error_events) >= 1, (
        f"Expected session.error on exit 1, got types: {[e.get('type') for e in events]}"
    )
    error_payload = str(error_events[-1])
    assert secret_token not in error_payload, (
        f"Token must be redacted in session.error payload, but found it in: {error_payload}"
    )
    assert "[REDACTED]" in error_payload, (
        f"Expected [REDACTED] in session.error payload, got: {error_payload}"
    )


# ---------------------------------------------------------------------------
# AC-4: timeout kills subprocess → session.error in < 4s, no zombie
# Covers FR-6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac4_timeout_kills_subprocess_and_emits_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocking subprocess must be killed after timeout; session.error emitted; no zombie."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import time
        time.sleep(100)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    # The resolved per-run timeout is passed in by the use case as ``timeout_s``;
    # the launcher no longer reads any timeout env var directly (issue #61).
    start = time.monotonic()
    await launcher.run(
        session_id="test-session-id",
        prompt="test",
        workspace=tmp_path,
        emit=capture,
        timeout_s=1,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 4.0, f"Timeout should fire within ~2s of the 1s limit; took {elapsed:.2f}s"

    error_events = [e for e in collected if e.get("type") == "session.error"]
    assert len(error_events) >= 1, (
        f"Expected session.error after timeout, got: {[e.get('type') for e in collected]}"
    )
    # The run() coroutine completing cleanly (no hang) is the primary zombie guard.
    # The elapsed check above also ensures the child process was killed promptly.


# ---------------------------------------------------------------------------
# AC-5: MAD_CLAUDE_CLI_BIN custom path is invoked (not $PATH claude)
# Covers FR-5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac5_custom_bin_path_is_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting MAD_CLAUDE_CLI_BIN must cause that exact binary to be invoked."""
    invoked_marker = tmp_path / "was_invoked"
    fake_bin = _make_executable_script(
        tmp_path / "my_fake_claude",
        f"""\
        import sys
        # Write a marker file to prove this script ran
        open("{invoked_marker}", "w").close()
        print("custom binary invoked")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))
    # Remove the real PATH so any 'claude' on PATH cannot be used
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin_dir"))

    launcher = ClaudeCLIProvider()
    await _collect_emit(launcher, prompt="test", workspace=tmp_path)

    assert invoked_marker.exists(), (
        f"Custom binary at {fake_bin} was not invoked — marker file not created. "
        "MAD_CLAUDE_CLI_BIN override must be respected."
    )


# ---------------------------------------------------------------------------
# AC-6: model is passed as --model <x> when set; absent when None (issue #55)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_ac6_model_flag_present_when_model_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``model`` is provided, ``--model <x>`` must appear in argv."""
    marker = tmp_path / "argv.txt"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        open("{marker}", "w").write(" ".join(sys.argv))
        print("done")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(
        session_id="s1", prompt="hello", workspace=tmp_path, emit=capture, model="claude-opus-4-5"
    )

    argv_text = marker.read_text()
    assert "--model" in argv_text, f"Expected --model in argv, got: {argv_text}"
    assert "claude-opus-4-5" in argv_text, f"Expected model id in argv, got: {argv_text}"


@pytest.mark.asyncio
async def test_claude_cli_ac6_model_flag_absent_when_model_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: when ``model=None``, ``--model`` must NOT appear in argv."""
    marker = tmp_path / "argv.txt"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        open("{marker}", "w").write(" ".join(sys.argv))
        print("done")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(
        session_id="s1", prompt="hello", workspace=tmp_path, emit=capture, model=None
    )

    argv_text = marker.read_text()
    assert "--model" not in argv_text, f"Expected --model absent from argv, got: {argv_text}"


# ---------------------------------------------------------------------------
# AC-10: provider-level rate-limit detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_rate_limit_api_retry_raises_rate_limit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """system/api_retry with error=rate_limit on stdout raises RateLimitError, no session.error."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "system", "subtype": "api_retry", "error": "rate_limit", "session_id": "conv-abc", "error_status": 429}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.reason == "rate_limit", (
        f"Expected reason='rate_limit', got: {excinfo.value.reason!r}"
    )
    assert excinfo.value.captured_id == "conv-abc", (
        f"Expected captured_id='conv-abc', got: {excinfo.value.captured_id!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted when RateLimitError is raised, got: {error_events}"
    )


@pytest.mark.asyncio
async def test_claude_cli_rate_limit_overloaded_api_retry_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """system/api_retry with error=overloaded raises RateLimitError with reason='overloaded'."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "system", "subtype": "api_retry", "error": "overloaded", "session_id": "conv-xyz", "error_status": 529}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.reason == "overloaded", (
        f"Expected reason='overloaded', got: {excinfo.value.reason!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted for overloaded rate-limit, got: {error_events}"
    )


@pytest.mark.asyncio
async def test_claude_cli_rate_limit_stderr_fallback_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stderr with a rate-limit pattern and no api_retry event raises RateLimitError."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys
        print("API Error: Repeated 529 Overloaded errors. The API is at capacity.", file=sys.stderr)
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted when stderr triggers RateLimitError, got: {error_events}"
    )
    assert excinfo.value.reason == "rate_limit"
    assert excinfo.value.captured_id is None


@pytest.mark.asyncio
async def test_claude_cli_billing_error_emits_session_error_not_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: billing stderr is terminal — session.error emitted, NOT RateLimitError."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys
        print("API Error: billing_error - payment required", file=sys.stderr)
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    # Must NOT raise RateLimitError — billing is non-retriable.
    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) >= 1, (
        f"Expected session.error for billing error (non-retriable), got: {captured_events}"
    )
    assert error_events[-1].get("exit_code") == 1, (
        f"session.error must carry exit_code=1, got: {error_events[-1]}"
    )


# ---------------------------------------------------------------------------
# AC-10 (follow-up): terminal-stdout rate-limit shape with CLAUDE_CODE_MAX_RETRIES=0.
#
# With internal retries disabled the CLI never emits system/api_retry. A real
# usage/session limit instead arrives as: a rate_limit_event (status="rejected"
# carrying resetsAt), an assistant message with error="rate_limit", and a
# terminal result (is_error=true, api_error_status=429) — all on STDOUT, with
# stderr empty and exit 1. The pre-fix detector keyed only on api_retry / stderr
# and silently drained the task. See issue #62 root-cause comment.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_rate_limit_terminal_stdout_result_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal-stdout 429 shape (rate_limit_event + assistant + result,
    empty stderr, exit 1) must raise RateLimitError, capture the conversation
    ID, carry a retry floor from resetsAt, and emit NO session.error."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json, time
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "conv-incident"}))
        resets = int(time.time()) + 3600
        print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": resets, "rateLimitType": "five_hour",
            "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled"}}))
        print(json.dumps({"type": "assistant", "message": {"model": "<synthetic>"}, "error": "rate_limit"}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 429, "result": "You've hit your session limit"}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.reason == "rate_limit", (
        f"Expected reason='rate_limit', got: {excinfo.value.reason!r}"
    )
    assert excinfo.value.captured_id == "conv-incident", (
        f"Expected captured_id from system/init, got: {excinfo.value.captured_id!r}"
    )
    # resetsAt was now+3600; the launcher converts it to a remaining-seconds
    # floor at raise time, so it lands just under an hour.
    assert excinfo.value.retry_after_floor_s == pytest.approx(3600, abs=120), (
        f"Expected ~3600 s retry floor from resetsAt, got: {excinfo.value.retry_after_floor_s!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted for a terminal-stdout 429, got: {error_events}"
    )


@pytest.mark.asyncio
async def test_claude_cli_terminal_result_non_429_emits_session_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a terminal result with is_error but a non-retriable status
    (500) is a real failure — session.error emitted, NOT RateLimitError."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 500, "result": "internal server error"}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    # Must NOT raise — a 500 is terminal, not a rate limit.
    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) >= 1, (
        f"Expected session.error for a non-429 terminal result, got: {captured_events}"
    )
    assert error_events[-1].get("exit_code") == 1, (
        f"session.error must carry exit_code=1, got: {error_events[-1]}"
    )


@pytest.mark.asyncio
async def test_claude_cli_rate_limit_without_resets_at_has_no_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin for the retry floor: a 429 result with NO rate_limit_event
    (so no resetsAt) still raises RateLimitError, but retry_after_floor_s is
    None — the dispatcher falls back to the plain backoff schedule."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "result", "is_error": True, "api_error_status": 429}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.retry_after_floor_s is None, (
        f"Expected no retry floor without resetsAt, got: {excinfo.value.retry_after_floor_s!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted for a 429 result, got: {error_events}"
    )


# ---------------------------------------------------------------------------
# Issue #72: a transient single-request 401 (authentication_failed) is NOT a
# real credential failure — the same OAuth token succeeds seconds later. It
# must ride the #62 backoff + conversation-resume path (RateLimitError) rather
# than draining the queue as a terminal session.error. ``billing_error`` stays
# terminal. session.error also carries the structured error/status/request_id
# instead of an empty string.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_transient_401_result_raises_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal result with api_error_status=401 (transient authentication_failed)
    raises RateLimitError with reason='authentication_failed', captures the
    conversation ID for resume, and emits NO session.error — so the dispatcher's
    #62 retry loop absorbs the blip instead of draining the queue."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "system", "subtype": "init", "session_id": "conv-401"}))
        print(json.dumps({"type": "assistant", "error": "authentication_failed",
            "request_id": "req_011CcGVY", "message": {"content": [
            {"type": "text", "text": "Failed to authenticate. API Error: 401"}]}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 401, "result": "Failed to authenticate. API Error: 401"}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.reason == "authentication_failed", (
        f"Expected reason='authentication_failed', got: {excinfo.value.reason!r}"
    )
    assert excinfo.value.captured_id == "conv-401", (
        f"Expected captured_id from system/init for resume, got: {excinfo.value.captured_id!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted for a transient 401, got: {error_events}"
    )


@pytest.mark.asyncio
async def test_claude_cli_transient_401_assistant_error_raises_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even without a terminal result event, an assistant message carrying
    error='authentication_failed' is enough to classify the turn as a transient
    401 — RateLimitError, no session.error."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "assistant", "error": "authentication_failed",
            "message": {"model": "<synthetic>"}}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    with pytest.raises(RateLimitError) as excinfo:
        await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    assert excinfo.value.reason == "authentication_failed", (
        f"Expected reason='authentication_failed', got: {excinfo.value.reason!r}"
    )
    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 0, (
        f"session.error must NOT be emitted for a transient 401, got: {error_events}"
    )


@pytest.mark.asyncio
async def test_claude_cli_structured_billing_error_emits_session_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a structured billing_error in the stream stays terminal —
    session.error emitted (NOT RateLimitError). Unlike a transient 401, a real
    billing failure must not be retried into the dispatcher's ceiling."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "assistant", "error": "billing_error",
            "message": {"model": "<synthetic>"}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 402, "result": "billing_error - payment required"}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    # Must NOT raise — billing is non-retriable, even arriving structured.
    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 1, (
        f"Expected one session.error for billing_error (non-retriable), got: {captured_events}"
    )
    assert error_events[-1].get("exit_code") == 1, (
        f"session.error must carry exit_code=1, got: {error_events[-1]}"
    )


@pytest.mark.asyncio
async def test_claude_cli_session_error_carries_structured_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal failure populates session.error.data from the last result:
    a non-empty error string plus api_error_status and request_id — diagnosable
    without scraping the agent.output log (issue #72 observability)."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys, json
        print(json.dumps({"type": "assistant", "error": "api_error",
            "request_id": "req_diag123", "message": {"model": "<synthetic>"}}))
        print(json.dumps({"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": 500, "result": "internal server error"}))
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 1, (
        f"Expected one session.error for a 500 failure, got: {captured_events}"
    )
    payload = error_events[-1]
    assert payload["error"] == "internal server error", (
        f"session.error.error must carry the structured result, got: {payload.get('error')!r}"
    )
    assert payload["api_error_status"] == 500, (
        f"session.error must carry api_error_status=500, got: {payload.get('api_error_status')!r}"
    )
    assert payload["request_id"] == "req_diag123", (
        f"session.error must carry request_id, got: {payload.get('request_id')!r}"
    )


@pytest.mark.asyncio
async def test_claude_cli_session_error_omits_detail_keys_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin for the structured detail: a plain stderr failure with no
    structured stream events falls back to the scrubbed stderr string and does
    NOT invent api_error_status / request_id keys."""
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        """\
        import sys
        print("boom: something broke", file=sys.stderr)
        sys.exit(1)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    captured_events: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        captured_events.append(event)

    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    error_events = [e for e in captured_events if e.get("type") == "session.error"]
    assert len(error_events) == 1, (
        f"Expected one session.error for a plain stderr failure, got: {captured_events}"
    )
    payload = error_events[-1]
    assert "boom: something broke" in payload["error"], (
        f"session.error.error must fall back to scrubbed stderr, got: {payload.get('error')!r}"
    )
    assert "api_error_status" not in payload, (
        f"api_error_status must be absent when no structured status was seen, got: {payload}"
    )
    assert "request_id" not in payload, (
        f"request_id must be absent when no structured request_id was seen, got: {payload}"
    )


@pytest.mark.asyncio
async def test_claude_cli_sets_max_retries_env_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLAUDE_CODE_MAX_RETRIES must be set to '0' in the subprocess environment (AC#2)."""
    marker = tmp_path / "max_retries.txt"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import os, sys
        open("{marker}", "w").write(os.environ.get("CLAUDE_CODE_MAX_RETRIES", "<unset>"))
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(session_id="s1", prompt="x", workspace=tmp_path, emit=capture)

    value = marker.read_text()
    assert value == "0", f"CLAUDE_CODE_MAX_RETRIES must be '0' in subprocess env, got: {value!r}"


# ---------------------------------------------------------------------------
# Issue #70: a single stdout line larger than asyncio's 64 KB default buffer
# must not raise LimitOverrunError and kill the task.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_long_stdout_line_emitted_without_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 KB single stdout line (far past asyncio's 64 KB default) must be
    emitted intact as agent.output and finish with session.status_idle — not
    die with LimitOverrunError (issue #70)."""
    line_len = 200_000  # > 64 KB asyncio default that used to overflow the buffer
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        print("z" * {line_len})
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    events = await _collect_emit(launcher, prompt="test", workspace=tmp_path)

    error_events = [e for e in events if e.get("type") == "session.error"]
    assert error_events == [], (
        f"a long stdout line must not produce session.error, got: {error_events}"
    )
    output_events = [e for e in events if e.get("type") == "agent.output"]
    assert len(output_events) == 1, (
        f"expected exactly one agent.output for the long line, got {len(output_events)}"
    )
    assert len(output_events[0]["line"]) == line_len, (
        f"long line must be emitted intact: expected {line_len} chars, "
        f"got {len(output_events[0]['line'])}"
    )
    idle_events = [e for e in events if e.get("type") == "session.status_idle"]
    assert len(idle_events) == 1, (
        f"expected session.status_idle after the long line, "
        f"got types: {[e.get('type') for e in events]}"
    )


# ---------------------------------------------------------------------------
# effort is passed as --effort <x> when set; absent when None (issue #60)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_cli_effort_flag_present_when_effort_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``effort`` is provided, ``--effort <x>`` must appear in argv."""
    marker = tmp_path / "argv.txt"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        open("{marker}", "w").write(" ".join(sys.argv))
        print("done")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(
        session_id="s1", prompt="hello", workspace=tmp_path, emit=capture, effort="high"
    )

    argv_text = marker.read_text()
    assert "--effort" in argv_text, f"Expected --effort in argv, got: {argv_text}"
    assert "high" in argv_text, f"Expected effort value in argv, got: {argv_text}"


@pytest.mark.asyncio
async def test_claude_cli_effort_flag_absent_when_effort_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: when ``effort=None``, ``--effort`` must NOT appear in argv."""
    marker = tmp_path / "argv.txt"
    fake_bin = _make_executable_script(
        tmp_path / "fake_claude",
        f"""\
        import sys
        open("{marker}", "w").write(" ".join(sys.argv))
        print("done")
        sys.exit(0)
        """,
    )
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    launcher = ClaudeCLIProvider()
    collected: list[dict] = []

    async def capture(event_type: str, event: dict) -> None:
        collected.append(event)

    await launcher.run(
        session_id="s1", prompt="hello", workspace=tmp_path, emit=capture, effort=None
    )

    argv_text = marker.read_text()
    assert "--effort" not in argv_text, f"Expected --effort absent from argv, got: {argv_text}"
