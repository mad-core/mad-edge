"""Security tests for the hard rules in CLAUDE.md / specs/infra/requirements.md.

FR-3  — Path traversal: mount_path values that escape the session workspace MUST be rejected.
FR-2  — Token hygiene: after clone, git remote -v must NOT contain the authorization_token.
NFR-2 — Token hygiene is also a non-functional constraint (tokens never persisted).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# FR-3 — Path traversal prevention
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_path_traversal_absolute_escape_is_rejected(client: TestClient, bare_repo: Path) -> None:
    """A mount_path that is an absolute path outside /workspace must be rejected with 400."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/etc/passwd",
                "authorization_token": "ghp_x",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, (
        f"Absolute escape mount_path must be rejected with 400, got {r.status_code}"
    )


@pytest.mark.smoke
def test_path_traversal_dotdot_is_rejected(client: TestClient, bare_repo: Path) -> None:
    """A mount_path using ../ to escape the workspace must be rejected with 400."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/../../../../tmp/escape",
                "authorization_token": "ghp_x",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, (
        f"Dot-dot escape mount_path must be rejected with 400, got {r.status_code}"
    )


@pytest.mark.smoke
def test_path_traversal_symlink_escape_is_rejected(client: TestClient, bare_repo: Path) -> None:
    """A mount_path of just /tmp (outside /workspace prefix) must be rejected with 400."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/tmp/injected",
                "authorization_token": "ghp_x",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, (
        f"mount_path /tmp/injected must be rejected with 400, got {r.status_code}"
    )


def test_path_traversal_file_resource_dotdot_rejected(
    client: TestClient,
) -> None:
    """A file resource with a dot-dot mount_path must also be rejected with 400."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "file",
                "content": "malicious",
                "mount_path": "/workspace/../../../etc/cron.d/evil",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, (
        f"Dot-dot file resource mount_path must be rejected, got {r.status_code}"
    )


def test_path_traversal_valid_workspace_path_is_accepted(
    client: TestClient, bare_repo: Path
) -> None:
    """A mount_path under /workspace/ must be accepted (positive control)."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/safe/repo",
                "authorization_token": "ghp_x",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200, (
        f"Valid /workspace/ mount_path must be accepted, got {r.status_code}"
    )


def test_path_traversal_root_is_rejected(client: TestClient, bare_repo: Path) -> None:
    """A mount_path of '/' must be rejected with 400 (Hard rule 3)."""
    payload = {
        "agent": {"name": "a", "system": "", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/",
                "authorization_token": "ghp_x",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, f"mount_path='/' must be rejected with 400, got {r.status_code}"


# ---------------------------------------------------------------------------
# FR-2 / NFR-2 — Token hygiene
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_token_stripped_from_remote_after_clone(client: TestClient, bare_repo: Path) -> None:
    """After cloning, git remote -v must not contain the authorization_token."""
    token = "ghp_supersecret_TOKEN_12345"
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
    local_path = Path(data["resources_mounted"][0]["local_path"])
    remote = subprocess.run(
        ["git", "-C", str(local_path), "remote", "-v"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert token not in remote, (
        f"authorization_token must be stripped from git remote after clone, but found it in:\n{remote}"
    )


@pytest.mark.smoke
def test_token_not_in_session_response(client: TestClient, bare_repo: Path) -> None:
    """The POST /v1/sessions response body must not echo the authorization_token."""
    token = "ghp_response_leak_TEST_99999"
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
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200
    assert token not in r.text, (
        "authorization_token must NOT appear anywhere in the session creation response"
    )


def test_token_not_in_stderr_of_launcher(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """FakeLauncher emits an agent.output event whose data contains the token;
    the JSONL persisted must NOT contain the token literal. (Hard rule 2)
    """
    token = "ghp_FAKE_LAUNCHER_TOKEN_secretXYZ"
    # Script the FakeLauncher to emit the token in an agent.output line
    fake_launcher.script(
        [
            [
                {"type": "agent.output", "line": f"Some output containing {token}"},
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
                "authorization_token": token,
            }
        ],
    }
    data = client.post("/v1/sessions", json=payload).json()
    session_id = data["session_id"]

    r = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "go"}]},
    )
    assert r.status_code in (200, 202)

    # Allow background task to complete
    time.sleep(0.2)

    log_path = Path("sessions") / f"{session_id}.jsonl"
    log_contents = log_path.read_text()
    assert token not in log_contents, (
        "authorization_token must NOT appear in the session log JSONL, even in agent.output events"
    )
