"""Unit tests for ListSessionsUseCase."""

from __future__ import annotations

import pytest

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.use_cases.list_sessions import ListSessionsUseCase
from support.sessions import FakeSessionRepository


def _make_session(session_id, status="created"):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_" + session_id,
        status=status,
    )


@pytest.fixture
def repo() -> FakeSessionRepository:
    return FakeSessionRepository()


def test_list_sessions_returns_all_in_memory(repo: FakeSessionRepository) -> None:
    sessions = {
        "sesn_001": _make_session("sesn_001", "created"),
        "sesn_002": _make_session("sesn_002", "idle"),
    }
    uc = ListSessionsUseCase(sessions_index=sessions, repo=repo)
    result = uc.execute()
    by_id = {s.session_id: s for s in result}
    assert by_id["sesn_001"].status == "created"
    assert by_id["sesn_002"].status == "idle"
    assert len(result) == 2


def test_list_sessions_includes_persisted_sessions_not_in_memory(
    repo: FakeSessionRepository,
) -> None:
    """Sessions persisted to JSONL but absent from the in-memory index
    must still appear — otherwise restarting the server drops them from
    /v1/sessions even though their event logs survive (hard rule 6).
    """
    repo.append_event("sesn_disk_1", "session.created", {"agent": "t"})
    repo.append_event("sesn_disk_1", "session.status_idle")
    repo.append_event("sesn_disk_2", "session.created", {"agent": "t"})

    uc = ListSessionsUseCase(sessions_index={}, repo=repo)
    result = uc.execute()

    by_id = {s.session_id: s for s in result}
    assert by_id["sesn_disk_1"].status == "idle"
    assert by_id["sesn_disk_2"].status == "created"
    assert len(result) == 2


def test_list_sessions_in_memory_status_wins_over_disk(
    repo: FakeSessionRepository,
) -> None:
    """If a session is both live and persisted, the live status is the
    source of truth — the in-memory entity reflects state transitions
    that may not yet have been written when the listing is served.
    """
    repo.append_event("sesn_001", "session.created", {"agent": "t"})
    sessions = {"sesn_001": _make_session("sesn_001", "running")}

    uc = ListSessionsUseCase(sessions_index=sessions, repo=repo)
    result = uc.execute()

    assert len(result) == 1
    assert result[0].status == "running"


def test_list_sessions_is_empty_when_no_sources(repo: FakeSessionRepository) -> None:
    uc = ListSessionsUseCase(sessions_index={}, repo=repo)
    assert uc.execute() == []
