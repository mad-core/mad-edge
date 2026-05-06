"""ListSessions use case."""
from __future__ import annotations

from dataclasses import dataclass

from mad.core.sessions.domain.entities.session import Session


@dataclass
class SessionSummary:
    session_id: str
    status: str


class ListSessionsUseCase:
    """Return a summary list of all known sessions."""

    def __init__(self, sessions_index: dict[str, Session]) -> None:
        self._sessions = sessions_index

    def execute(self) -> list[SessionSummary]:
        return [
            SessionSummary(session_id=sid, status=s.status)
            for sid, s in self._sessions.items()
        ]
