from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import mad.adapters.outbound.persistence.jsonl_session_repository as _adapter_log
import mad.adapters.outbound.persistence.local_workspace_provisioner as _adapter_workspaces
from mad.adapters.inbound.http import create_app
from support.launchers import ScriptedLauncher

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_launcher() -> ScriptedLauncher:
    """Return a ScriptedLauncher; injected into create_app via launcher_factory."""
    return ScriptedLauncher()


@pytest.fixture
def client(
    fake_launcher: ScriptedLauncher,
    tmp_sessions_dir: Path,
    tmp_workspaces_dir: Path,
) -> TestClient:
    return TestClient(create_app(launcher_factory=lambda name: fake_launcher))


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
    """Point the session log directory at a ``tmp_path`` subdirectory.

    Drives the public ``MAD_SESSIONS_DIR`` env var (the same knob an operator
    sets in production) so all persistence code resolves to the tmp directory
    instead of the CWD-relative ``sessions/`` dir.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv(_adapter_log.SESSIONS_DIR_ENV, str(sessions))
    return sessions


@pytest.fixture
def tmp_workspaces_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``LocalWorkspaceProvisioner`` workspaces into ``tmp_path``.

    The provisioner builds workspace paths via ``workspace_path(session_id)``
    which resolves under ``tempfile.gettempdir()``. Without this fixture
    every integration test leaks a ``mad_<session_id>/`` directory into
    ``$TMPDIR`` that ``pytest`` never cleans up.
    """
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    monkeypatch.setattr(
        _adapter_workspaces,
        "workspace_path",
        lambda session_id: workspaces / f"mad_{session_id}",
    )
    return workspaces
