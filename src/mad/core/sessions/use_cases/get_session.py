"""GetSession use case.

Retrieves a session by ID. Falls back to JSONL rehydration if the session
is not in the in-memory index (implements hard rule 6 — JSONL as source of truth).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.domain.rehydrate import rehydrate_from_events
from mad.core.sessions.ports.outbound.session_repository import SessionRepository


@dataclass
class GetSessionOutput:
    session_id: str
    status: str
    workspace: str
    events: list[dict[str, Any]]
    priority: int
    created_at: datetime
    updated_at: datetime
    last_conversation_id: str | None = None


class GetSessionUseCase:
    """Retrieve a session, rehydrating from JSONL if not in memory."""

    def __init__(
        self,
        repo: SessionRepository,
        sessions_index: dict[str, Session],
    ) -> None:
        self._repo = repo
        self._sessions = sessions_index

    def execute(self, session_id: str) -> GetSessionOutput:
        session = self._sessions.get(session_id)

        if session is None:
            if not self._repo.exists(session_id):
                raise SessionNotFound(session_id)
            session = rehydrate_from_events(session_id, self._repo.read_events(session_id))
            self._sessions[session_id] = session

        events = self._repo.read_events(session_id)
        return GetSessionOutput(
            session_id=session_id,
            status=session.status,
            workspace=session.workspace,
            events=events,
            priority=session.priority,
            created_at=session.created_at,
            updated_at=session.updated_at,
            last_conversation_id=session.last_conversation_id,
        )
