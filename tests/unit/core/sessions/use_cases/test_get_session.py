"""Unit tests for GetSessionUseCase."""

from __future__ import annotations

import pytest

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.get_session import GetSessionUseCase


class FakeRepo:
    def __init__(self):
        self._events: dict[str, list[dict]] = {}

    def append_event(self, session_id, event_type, data=None):
        event = {"type": event_type, **(data or {})}
        self._events.setdefault(session_id, []).append(event)
        return event

    def read_events(self, session_id):
        return self._events.get(session_id, [])

    def exists(self, session_id):
        return session_id in self._events


def _make_session(session_id="sesn_abc", status="created"):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_sesn_abc",
        status=status,
    )


def test_get_session_from_memory(tmp_path):
    sessions = {"sesn_abc": _make_session()}
    repo = FakeRepo()
    repo.append_event("sesn_abc", "session.created", {"agent": "t"})
    uc = GetSessionUseCase(repo=repo, sessions_index=sessions)
    out = uc.execute("sesn_abc")
    assert out.session_id == "sesn_abc"
    assert out.status == "created"
    assert len(out.events) >= 1


def test_get_session_not_found_raises():
    sessions = {}
    repo = FakeRepo()
    uc = GetSessionUseCase(repo=repo, sessions_index=sessions)
    with pytest.raises(SessionNotFound):
        uc.execute("sesn_missing")


def test_get_session_rehydrates_from_jsonl():
    """If not in memory but in JSONL, it rehydrates."""
    sessions: dict[str, Session] = {}
    repo = FakeRepo()
    repo.append_event("sesn_rehydrated", "session.created", {"agent": "t"})
    repo.append_event("sesn_rehydrated", "session.status_idle", {})
    uc = GetSessionUseCase(repo=repo, sessions_index=sessions)
    out = uc.execute("sesn_rehydrated")
    assert out.session_id == "sesn_rehydrated"
    assert out.status == "idle"
    # Also cached in memory
    assert "sesn_rehydrated" in sessions


@pytest.mark.parametrize(
    "lifecycle_event,expected_status",
    [
        ("session.status_running", "running"),
        ("session.status_idle", "idle"),
        ("session.error", "error"),
    ],
)
def test_get_session_rehydrates_status_from_lifecycle_event(
    lifecycle_event: str, expected_status: str
):
    """Rehydration must map each lifecycle event to the correct session status."""
    sessions: dict[str, Session] = {}
    repo = FakeRepo()
    repo.append_event("sesn_lc", "session.created", {"agent": "t"})
    repo.append_event("sesn_lc", lifecycle_event, {})
    uc = GetSessionUseCase(repo=repo, sessions_index=sessions)
    out = uc.execute("sesn_lc")
    assert out.status == expected_status


def test_get_session_returns_events():
    sessions = {"sesn_xyz": _make_session("sesn_xyz", "idle")}
    repo = FakeRepo()
    repo.append_event("sesn_xyz", "session.created", {"agent": "t"})
    repo.append_event("sesn_xyz", "user.message", {"content": "hello"})
    uc = GetSessionUseCase(repo=repo, sessions_index=sessions)
    out = uc.execute("sesn_xyz")
    assert len(out.events) == 2
