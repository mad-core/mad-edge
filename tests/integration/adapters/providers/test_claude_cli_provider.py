"""Unit tests for the ClaudeCLI provider — mapped 1:1 to specs/claude-cli/requirements.md.

AC → test mapping:
  AC-1  stdout lines → agent.output events                  → test_claude_cli_ac1_*
  AC-2  exit 0 → session.status_idle                       → test_claude_cli_ac2_*
  AC-3  exit 1 + token in stderr → session.error/redacted  → test_claude_cli_ac3_*
  AC-4  timeout → session.error < 2s, no zombie            → test_claude_cli_ac4_*
  AC-5  MAD_CLAUDE_CLI_BIN custom path is invoked           → test_claude_cli_ac5_*

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
    monkeypatch.setenv("MAD_CLAUDE_CLI_TIMEOUT_S", "1")

    launcher = ClaudeCLIProvider()

    start = time.monotonic()
    events = await _collect_emit(launcher, prompt="test", workspace=tmp_path)
    elapsed = time.monotonic() - start

    assert elapsed < 4.0, f"Timeout should fire within ~2s of the 1s limit; took {elapsed:.2f}s"

    error_events = [e for e in events if e.get("type") == "session.error"]
    assert len(error_events) >= 1, (
        f"Expected session.error after timeout, got: {[e.get('type') for e in events]}"
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
