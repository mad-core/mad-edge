"""GetSession use case.

Retrieves a session by ID. Falls back to JSONL rehydration if the session
is not in the in-memory index (implements hard rule 6 — JSONL as source of truth).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.ports.outbound.session_repository import SessionRepository


@dataclass
class GetSessionOutput:
    session_id: str
    status: str
    workspace: str
    events: list[dict[str, Any]]


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
            # Attempt JSONL rehydration (hard rule 6)
            if not self._repo.exists(session_id):
                raise SessionNotFound(session_id)
            session = _rehydrate_from_events(session_id, self._repo)
            # Cache in memory for subsequent requests
            self._sessions[session_id] = session

        events = self._repo.read_events(session_id)
        return GetSessionOutput(
            session_id=session_id,
            status=session.status,
            workspace=session.workspace,
            events=events,
        )


def _rehydrate_from_events(session_id: str, repo: SessionRepository) -> Session:
    """Build a minimal Session entity from the persisted JSONL events."""
    events = repo.read_events(session_id)
    agent: dict[str, Any] = {}
    workspace = ""
    status = "created"

    for event in events:
        etype = event.get("type", "")
        if etype == "session.created":
            # agent name stored but not the full dict — reconstruct minimal
            agent = {"name": event.get("agent", ""), "provider": "unknown"}
        elif etype == "session.status_running":
            status = "running"
        elif etype == "session.status_idle":
            status = "idle"
        elif etype == "session.error":
            status = "error"

    return Session(
        session_id=session_id,
        agent=agent,
        workspace=workspace,
        status=status,
    )
