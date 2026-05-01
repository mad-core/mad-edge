"""Unit tests for CreateSessionUseCase.

Uses fake port implementations — no HTTP, no filesystem, no real git.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mad.core.domain.entities.session import Session
from mad.core.domain.exceptions.base import PathTraversalError
from mad.core.use_cases.sessions.create_session import (
    CreateSessionInput,
    CreateSessionUseCase,
    ResourceSpec,
)

# ---------------------------------------------------------------------------
# Fake ports
# ---------------------------------------------------------------------------


class FakeSessionRepository:
    def __init__(self):
        self.events: list[dict] = []

    def append_event(self, session_id: str, event_type: str, data: dict | None = None) -> dict:
        event = {"type": event_type, "session_id": session_id, **(data or {})}
        self.events.append(event)
        return event

    def read_events(self, session_id: str) -> list[dict]:
        return [e for e in self.events if e.get("session_id") == session_id]

    def exists(self, session_id: str) -> bool:
        return any(e.get("session_id") == session_id for e in self.events)


class FakeProvisioner:
    def __init__(self, workspace_root: Path):
        self._root = workspace_root
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.files_written: list[tuple[str, str]] = []
        self.repos_cloned: list[tuple[str, str]] = []

    def create(self, session_id: str) -> Path:
        self.created.append(session_id)
        p = self._root / session_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def destroy(self, session_id: str) -> None:
        self.destroyed.append(session_id)

    def materialize_github_repo(
        self, workspace: Path, mount_path: str, repo_url: str, token: str | None
    ) -> None:
        self.repos_cloned.append((mount_path, repo_url))

    def materialize_file(self, workspace: Path, mount_path: str, content: str) -> None:
        self.files_written.append((mount_path, content))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    return FakeSessionRepository()


@pytest.fixture
def provisioner(tmp_path):
    return FakeProvisioner(tmp_path)


@pytest.fixture
def use_case(repo, provisioner):
    sessions: dict[str, Session] = {}
    idempotency: dict[str, str] = {}
    return (
        CreateSessionUseCase(
            repo=repo,
            provisioner=provisioner,
            sessions_index=sessions,
            idempotency_index=idempotency,
        ),
        sessions,
        idempotency,
    )


def test_create_session_happy_path(use_case):
    uc, sessions, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "test", "provider": "fake"},
        resources=[],
    )
    output = uc.execute(payload)
    assert output.session.status == "created"
    assert output.session.session_id in sessions


def test_create_session_emits_created_event(use_case, repo):
    uc, _, _ = use_case
    payload = CreateSessionInput(agent={"name": "myagent", "provider": "fake"}, resources=[])
    output = uc.execute(payload)
    created_events = [e for e in repo.events if e["type"] == "session.created"]
    assert len(created_events) == 1
    assert created_events[0]["agent"] == "myagent"


def test_invalid_mount_path_raises(use_case):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[ResourceSpec(type="file", mount_path="/etc/passwd", content="evil")],
    )
    with pytest.raises(PathTraversalError):
        uc.execute(payload)


def test_idempotency_returns_same_session(use_case):
    uc, sessions, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[],
        idempotency_key="key-abc",
    )
    out1 = uc.execute(payload)
    out2 = uc.execute(payload)
    assert out1.session.session_id == out2.session.session_id
    # Only one session was created
    assert len(sessions) == 1


def test_file_resource_is_materialized(use_case, provisioner):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[ResourceSpec(type="file", mount_path="/workspace/code.py", content="x=1")],
    )
    uc.execute(payload)
    assert len(provisioner.files_written) == 1
    mp, content = provisioner.files_written[0]
    assert mp == "/workspace/code.py"
    assert content == "x=1"


def test_github_repo_resource_is_materialized(use_case, provisioner):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[
            ResourceSpec(
                type="github_repository",
                mount_path="/workspace/repo",
                url="https://github.com/test/repo",
                authorization_token="ghp_test",
            )
        ],
    )
    uc.execute(payload)
    assert len(provisioner.repos_cloned) == 1
    mp, url = provisioner.repos_cloned[0]
    assert mp == "/workspace/repo"


def test_tokens_stored_in_session_for_redaction(use_case):
    uc, sessions, _ = use_case
    token = "ghp_mysecret"
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[
            ResourceSpec(
                type="github_repository",
                mount_path="/workspace/repo",
                url="https://github.com/test/repo",
                authorization_token=token,
            )
        ],
    )
    output = uc.execute(payload)
    assert token in output.session.tokens_to_redact
