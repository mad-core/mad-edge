"""Hard rule 1 — Native tool use only.

Mad streams agent stdout as agent.output events and never parses or emits
agent.tool_use events. This file verifies that even if the agent prints text
that looks like a tool call, Mad does not interpret it and produces no
agent.tool_use entries in the session log.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.smoke
def test_launcher_output_lines_emitted_as_agent_output(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """agent.output lines from the launcher are streamed as-is; agent.tool_use MUST NOT appear.

    The FakeLauncher scripts 3 agent.output events (including one that looks like
    a free-text tool call) plus a terminal session.status_idle. The test asserts:
      - All 3 agent.output events are recorded in the session log.
      - No agent.tool_use event exists in the log (Mad never parses agent output).
    """
    tool_call_lookalike = '<tool>bash</tool><input>{"command": "rm -rf /"}</input>'
    fake_launcher.script(
        [
            [
                {"type": "agent.output", "line": "Line one from agent"},
                {"type": "agent.output", "line": tool_call_lookalike},
                {"type": "agent.output", "line": "Line three from agent"},
                {"type": "session.status_idle", "stop_reason": "end_turn"},
            ]
        ]
    )
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
                "authorization_token": "ghp_x",
            }
        ],
    }
    data = client.post("/v1/sessions", json=payload).json()
    session_id = data["session_id"]

    r = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "stream output please"}]},
    )
    assert r.status_code in (200, 202)

    # Allow the background task to complete (FakeLauncher is instant)
    time.sleep(0.2)

    log_path = Path("sessions") / f"{session_id}.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]

    output_events = [e for e in lines if e.get("type") == "agent.output"]
    assert len(output_events) == 3, (
        f"Expected 3 agent.output events in log, got {len(output_events)}: {output_events}"
    )

    tool_use_events = [e for e in lines if e.get("type") == "agent.tool_use"]
    assert len(tool_use_events) == 0, (
        f"Mad must never emit agent.tool_use — free-text tool calls must be ignored; "
        f"got: {tool_use_events}"
    )
