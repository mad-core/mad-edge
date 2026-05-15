"""Integration tests for #16: ClaudeCLIProvider exports MAD_* env vars.

forward.sh inside the workspace reads these vars to identify the Mad
session, locate the UDS, and tag emitted events with the provider name.
If any var is missing the hook silently no-ops, so this test pins the
exact env contract.
"""

from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path

import pytest

from mad.adapters.outbound.agents.claude_cli import ClaudeCLIProvider


def _make_env_dump_bin(target_dir: Path, dump_path: Path) -> Path:
    """Build a fake `claude` binary that dumps a subset of its env to a JSON file."""
    src = textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json, os, sys
        keys = ["MAD_SESSION_ID", "MAD_HOOK_SOCKET", "MAD_PROVIDER"]
        json.dump({{k: os.environ.get(k) for k in keys}}, open({str(dump_path)!r}, "w"))
        print("ok")
        sys.exit(0)
        """
    )
    bin_path = target_dir / "fake_claude"
    bin_path.write_text(src)
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_path


async def _run_capturing(
    launcher: ClaudeCLIProvider,
    *,
    session_id: str,
    workspace: Path,
) -> list[dict]:
    collected: list[dict] = []

    async def emit(event_type: str, data: dict | None) -> None:
        collected.append({"type": event_type, **(data or {})})

    await launcher.run(session_id=session_id, prompt="ignored", workspace=workspace, emit=emit)
    return collected


@pytest.mark.asyncio
async def test_subprocess_receives_mad_session_id_hook_socket_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three vars must be present in the spawned subprocess env, and the
    values must match what the launcher promises (session_id passthrough,
    explicit socket path from MAD_HOOK_SOCKET, provider literal "claude_cli").
    """
    dump_path = tmp_path / "env.json"
    fake_bin = _make_env_dump_bin(tmp_path, dump_path)
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))
    monkeypatch.setenv("MAD_HOOK_SOCKET", "/tmp/mad-test/hooks.sock")

    await _run_capturing(
        ClaudeCLIProvider(),
        session_id="sesn_test_42",
        workspace=tmp_path,
    )

    assert dump_path.is_file(), "fake binary did not run; env was not captured"
    captured = json.loads(dump_path.read_text())
    assert captured["MAD_SESSION_ID"] == "sesn_test_42", (
        f"MAD_SESSION_ID mismatch: got {captured['MAD_SESSION_ID']!r}"
    )
    assert captured["MAD_HOOK_SOCKET"] == "/tmp/mad-test/hooks.sock", (
        f"MAD_HOOK_SOCKET mismatch: got {captured['MAD_HOOK_SOCKET']!r}"
    )
    assert captured["MAD_PROVIDER"] == "claude_cli", (
        f"MAD_PROVIDER mismatch: got {captured['MAD_PROVIDER']!r}"
    )


@pytest.mark.asyncio
async def test_run_does_not_mutate_callers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: env vars must only be set on the subprocess, never on
    the calling process. A leak here would surface as cross-session
    contamination once mad serve handles concurrent runs.
    """
    import os

    monkeypatch.delenv("MAD_SESSION_ID", raising=False)
    monkeypatch.delenv("MAD_HOOK_SOCKET", raising=False)
    monkeypatch.delenv("MAD_PROVIDER", raising=False)

    dump_path = tmp_path / "env.json"
    fake_bin = _make_env_dump_bin(tmp_path, dump_path)
    monkeypatch.setenv("MAD_CLAUDE_CLI_BIN", str(fake_bin))

    await _run_capturing(ClaudeCLIProvider(), session_id="sesn_leak_check", workspace=tmp_path)

    assert "MAD_SESSION_ID" not in os.environ, "MAD_SESSION_ID leaked into parent env"
    assert "MAD_PROVIDER" not in os.environ, "MAD_PROVIDER leaked into parent env"
    # MAD_HOOK_SOCKET is allowed to be pre-set by the operator, but the launcher
    # MUST NOT introduce it if absent — our setup deleted it above.
    assert "MAD_HOOK_SOCKET" not in os.environ, "MAD_HOOK_SOCKET leaked into parent env"
