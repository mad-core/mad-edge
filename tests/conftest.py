from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mad.adapters.outbound.persistence.jsonl_session_repository as _adapter_log
from mad.adapters.inbound.http import create_app
from mad.adapters.outbound.agents import factory
from mad.adapters.outbound.agents.fake import FakeLauncher

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_launcher(monkeypatch: pytest.MonkeyPatch) -> FakeLauncher:
    """Return a FakeLauncher and monkeypatch get_launcher to return it."""
    launcher = FakeLauncher()
    monkeypatch.setattr(factory, "get_launcher", lambda name: launcher)
    return launcher


# Keep fake_provider as an alias so any remaining references don't break
# immediately — but new tests MUST use fake_launcher.
@pytest.fixture
def fake_provider(fake_launcher: FakeLauncher) -> FakeLauncher:
    return fake_launcher


@pytest.fixture
def client(fake_launcher: FakeLauncher) -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A local git bare repo with one commit on `main`. Use as a clone source."""
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed repo\n")
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
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def _session_payload(bare_repo: Path) -> dict:
    return {
        "agent": {
            "name": "test-agent",
            "system": "You are a test agent.",
            "provider": "fake_scripted",
        },
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
                "authorization_token": "ghp_fake_token_xxx",
                "checkout": {"type": "branch", "name": "main"},
            }
        ],
    }


@pytest.fixture
def session_payload(bare_repo: Path) -> dict:
    return _session_payload(bare_repo)


@pytest.fixture
def tmp_sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Monkeypatch SESSIONS_DIR to a tmp_path subdirectory.

    Patches the adapter module (canonical location) so all persistence code
    writes to the tmp directory instead of the CWD-relative 'sessions/' dir.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(_adapter_log, "SESSIONS_DIR", sessions)
    return sessions
