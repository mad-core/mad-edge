"""Unit tests for ListSessionsUseCase."""

from __future__ import annotations

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.use_cases.list_sessions import ListSessionsUseCase


def _make_session(session_id, status="created"):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_" + session_id,
        status=status,
    )


def test_list_sessions_returns_all():
    sessions = {
        "sesn_001": _make_session("sesn_001", "created"),
        "sesn_002": _make_session("sesn_002", "idle"),
    }
    uc = ListSessionsUseCase(sessions_index=sessions)
    result = uc.execute()
    assert len(result) == 2
    by_id = {s.session_id: s for s in result}
    assert "sesn_001" in by_id
    assert "sesn_002" in by_id
    assert by_id["sesn_001"].status == "created"
    assert by_id["sesn_002"].status == "idle"
