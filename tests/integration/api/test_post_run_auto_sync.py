"""Integration tests for issue #8: optional base_branch on session create
and post-run auto-sync second launcher invocation.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app


def _bare_repo_with_branches(tmp_path: Path, branches: list[str]) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", branches[0], str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    for branch in branches[1:]:
        subprocess.run(
            ["git", "-C", str(seed), "branch", branch],
            check=True,
            capture_output=True,
        )
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


@pytest.fixture
def repo_with_branches(tmp_path: Path) -> Path:
    return _bare_repo_with_branches(tmp_path, ["main", "develop"])


def test_create_session_with_base_branch_checks_it_out(
    client: TestClient, repo_with_branches: Path
) -> None:
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "base_branch": "develop",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{repo_with_branches}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200
    local = Path(r.json()["resources_mounted"][0]["local_path"])
    head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() == "develop"


def test_create_session_with_unknown_base_branch_returns_400(
    client: TestClient, repo_with_branches: Path
) -> None:
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "base_branch": "no-such-branch",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{repo_with_branches}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400
    assert "no-such-branch" in r.json()["detail"]


def test_base_branch_persisted_in_session_log(client: TestClient, repo_with_branches: Path) -> None:
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "base_branch": "develop",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{repo_with_branches}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    create = client.post("/v1/sessions", json=payload)
    assert create.status_code == 200
    created = create.json()
    session_id = created["session_id"]

    # The Session entity persists base_branch; the JSONL log records
    # session.created. GET must succeed (shape) AND the workspace clone
    # must actually be on the requested base_branch (value).
    r = client.get(f"/v1/sessions/{session_id}")
    assert r.status_code == 200

    local_path = Path(created["resources_mounted"][0]["local_path"])
    head = subprocess.run(
        ["git", "-C", str(local_path), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert head.stdout.strip() == "develop", (
        f"workspace must be checked out on base_branch=develop; got {head.stdout!r}"
    )


def test_post_run_auto_sync_invokes_second_launcher_run(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """With auto-sync opted in, the launcher runs twice per user.message: primary
    + auto-sync. Auto-sync is off by default (issue #109), so the session sets
    ``auto_sync: true`` to exercise the post-run publish path."""
    fake_launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    session_id = client.post(
        "/v1/sessions", json={**session_payload, "auto_sync": True}
    ).json()["session_id"]
    r = client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "do work"},
    )
    assert r.status_code == 200

    # Poll until both launcher runs complete (primary + auto-sync).
    deadline = time.monotonic() + 5.0
    while len(fake_launcher.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert len(fake_launcher.calls) == 2, (
        f"expected 2 launcher invocations (primary + auto-sync), got {len(fake_launcher.calls)}"
    )
    primary_prompt = fake_launcher.calls[0]["prompt"]
    auto_sync_prompt = fake_launcher.calls[1]["prompt"]
    assert primary_prompt == "do work"
    assert "auto-sync" in auto_sync_prompt.lower()
    assert ".claude/settings.local.json" in auto_sync_prompt
    assert ".claude/settings.json" in auto_sync_prompt


def test_post_run_auto_sync_uses_base_branch_in_prompt(
    repo_with_branches: Path, tmp_sessions_dir: Path, tmp_workspaces_dir: Path
) -> None:
    """The auto-sync prompt must reference the session's base_branch. Auto-sync is
    off by default (issue #109), so the session opts in with ``auto_sync: true``."""
    from support.launchers import ScriptedLauncher

    fake = ScriptedLauncher()
    fake.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    client = TestClient(create_app(launcher_factory=lambda name: fake))

    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "base_branch": "develop",
        "auto_sync": True,
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{repo_with_branches}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    session_id = client.post("/v1/sessions", json=payload).json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "go"},
    )

    deadline = time.monotonic() + 2.0
    while len(fake.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert len(fake.calls) == 2
    auto_sync_prompt = fake.calls[1]["prompt"]
    assert "develop" in auto_sync_prompt
    assert f"mad/{session_id}" in auto_sync_prompt


def test_post_run_auto_sync_runs_even_when_primary_fails(
    client: TestClient, fake_launcher, session_payload: dict, tmp_sessions_dir: Path
) -> None:
    """Auto-sync MUST fire even after the primary run reports session.error.
    Auto-sync is off by default (issue #109), so the session opts in explicitly."""
    fake_launcher.script(
        [
            [{"type": "session.error", "error": "boom"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    session_id = client.post(
        "/v1/sessions", json={**session_payload, "auto_sync": True}
    ).json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/messages",
        json={"content": "go"},
    )

    # Poll until both launcher runs complete (primary + auto-sync).
    deadline = time.monotonic() + 5.0
    while len(fake_launcher.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)

    assert len(fake_launcher.calls) == 2

    log_path = tmp_sessions_dir / f"{session_id}.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    types = [e["type"] for e in lines]
    assert types.count("session.error") >= 1
    # The auto-sync run completed successfully → the final state for this
    # second run is session.status_idle.
    assert "session.status_idle" in types
