"""Unit tests for CreateSessionUseCase.

Uses fake port implementations — no HTTP, no filesystem, no real git.
"""

from __future__ import annotations

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import PathTraversalError
from mad.core.sessions.use_cases.create_session import (
    CreateSessionInput,
    CreateSessionUseCase,
    ResourceSpec,
)
from support.events import FakeEventBus
from support.sessions import FakeProvisioner, FakeSessionRepository


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo():
    return FakeSessionRepository()


@pytest.fixture
def bus():
    return FakeEventBus()


@pytest.fixture
def provisioner(tmp_path):
    return FakeProvisioner(tmp_path)


@pytest.fixture
def use_case(repo, bus, provisioner):
    sessions: dict[str, Session] = {}
    idempotency: dict[str, str] = {}
    emitter = EventEmitter(store=repo, bus=bus)
    return (
        CreateSessionUseCase(
            provisioner=provisioner,
            sessions_index=sessions,
            idempotency_index=idempotency,
            emitter=emitter,
        ),
        sessions,
        idempotency,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_create_session_happy_path(use_case):
    uc, sessions, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "test", "provider": "fake"},
        resources=[],
    )
    output = await uc.execute(payload)
    assert output.session.status == "created"
    assert output.session.session_id in sessions


async def test_create_session_emits_created_event(use_case, repo):
    uc, _, _ = use_case
    payload = CreateSessionInput(agent={"name": "myagent", "provider": "fake"}, resources=[])
    output = await uc.execute(payload)
    created_events = [e for e in repo.events if e["type"] == "session.created"]
    assert len(created_events) == 1
    assert created_events[0]["agent"] == "myagent"


async def test_create_session_publishes_created_event_to_bus(use_case, bus):
    """session.created must be published to the EventBus via the emitter."""
    uc, _, _ = use_case
    payload = CreateSessionInput(agent={"name": "myagent", "provider": "fake"}, resources=[])
    output = await uc.execute(payload)
    created_on_bus = [e for e in bus.published if e.type == "session.created"]
    assert len(created_on_bus) == 1
    assert created_on_bus[0].session_id == output.session.session_id
    assert created_on_bus[0].data.get("agent") == "myagent"


async def test_invalid_mount_path_raises(use_case):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[ResourceSpec(type="file", mount_path="/etc/passwd", content="evil")],
    )
    with pytest.raises(PathTraversalError):
        await uc.execute(payload)


async def test_idempotency_returns_same_session(use_case):
    uc, sessions, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[],
        idempotency_key="key-abc",
    )
    out1 = await uc.execute(payload)
    out2 = await uc.execute(payload)
    assert out1.session.session_id == out2.session.session_id
    # Only one session was created
    assert len(sessions) == 1


async def test_file_resource_is_materialized(use_case, provisioner):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[ResourceSpec(type="file", mount_path="/workspace/code.py", content="x=1")],
    )
    await uc.execute(payload)
    assert len(provisioner.files_written) == 1
    mp, content = provisioner.files_written[0]
    assert mp == "/workspace/code.py"
    assert content == "x=1"


async def test_github_repo_resource_is_materialized(use_case, provisioner):
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
    await uc.execute(payload)
    assert len(provisioner.repos_cloned) == 1
    mp, url, _ = provisioner.repos_cloned[0]
    assert mp == "/workspace/repo"


async def test_base_branch_propagates_to_provisioner_and_session(use_case, provisioner):
    """CreateSession must forward base_branch to the provisioner and persist
    it on the resulting Session entity (issue #8)."""
    uc, sessions, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[
            ResourceSpec(
                type="github_repository",
                mount_path="/workspace/repo",
                url="https://github.com/test/repo",
            )
        ],
        base_branch="develop",
    )
    output = await uc.execute(payload)
    assert provisioner.repos_cloned[0][2] == "develop"
    assert output.session.base_branch == "develop"
    assert sessions[output.session.session_id].base_branch == "develop"


async def test_base_branch_defaults_to_none_when_omitted(use_case, provisioner):
    uc, _, _ = use_case
    payload = CreateSessionInput(
        agent={"name": "a", "provider": "fake"},
        resources=[
            ResourceSpec(
                type="github_repository",
                mount_path="/workspace/repo",
                url="https://github.com/test/repo",
            )
        ],
    )
    output = await uc.execute(payload)
    assert provisioner.repos_cloned[0][2] is None
    assert output.session.base_branch is None


async def test_tokens_stored_in_session_for_redaction(use_case):
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
    output = await uc.execute(payload)
    assert token in output.session.tokens_to_redact
