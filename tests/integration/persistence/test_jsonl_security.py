"""JSONL session log security tests.

NFR-2 / Hard rule 2 — Token hygiene: authorization tokens must never appear
in the persisted JSONL session log.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.mark.smoke
def test_token_not_in_session_log(
    client: TestClient, bare_repo: Path, tmp_sessions_dir: Path
) -> None:
    """The JSONL session log must not contain the authorization_token at any point."""
    token = "ghp_log_leak_TOKEN_77777"
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
                "authorization_token": token,
            }
        ],
    }
    data = client.post("/v1/sessions", json=payload).json()
    session_id = data["session_id"]

    log_path = tmp_sessions_dir / f"{session_id}.jsonl"
    assert log_path.exists(), "session log must exist"
    log_contents = log_path.read_text()
    assert token not in log_contents, "authorization_token must NOT appear in the session log JSONL"
