from __future__ import annotations

import subprocess
from collections import deque
from pathlib import Path
from typing import Callable, Awaitable

import pytest
from fastapi.testclient import TestClient

from mad.api.app import create_app
from mad.providers import factory


# ---------------------------------------------------------------------------
# FakeLauncher — test double for the new AgentLauncher protocol
# ---------------------------------------------------------------------------

class FakeLauncher:
    """Test double for AgentLauncher.

    Script a sequence of runs via .script(runs). Each element of `runs` is a
    list of event dicts that will be emitted (in order) for one call to run().

    If the scripted queue is exhausted, a default session.status_idle event is
    emitted so tests that don't care about the response still terminate cleanly.
    """

    def __init__(self) -> None:
        self._queue: deque[list[dict]] = deque()

    def script(self, runs: list[list[dict]]) -> None:
        """Pre-load the sequence of event-lists for upcoming run() calls."""
        self._queue = deque(runs)

    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict], Awaitable[None]],
    ) -> None:
        """Emit the next scripted run's events, or a default idle event."""
        if self._queue:
            events = self._queue.popleft()
        else:
            events = [{"type": "session.status_idle", "stop_reason": "end_turn"}]

        for event in events:
            await emit(event["type"], event)


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
        ["git", "-C", str(seed), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
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
