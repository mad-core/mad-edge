"""ListSessions use case."""
from __future__ import annotations

from dataclasses import dataclass

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.rehydrate import rehydrate_from_events
from mad.core.sessions.ports.outbound.session_repository import SessionRepository


@dataclass
class SessionSummary:
    session_id: str
    status: str


class ListSessionsUseCase:
    """Return a summary of every known session.

    Sources are unioned in this order:
      1. In-memory index (live sessions in this process).
      2. Persisted JSONL logs on disk (hard rule 6 — log is source of truth).

    Sessions found only on disk are rehydrated from their event stream so
    listings survive process restarts.
    """

    def __init__(
        self,
        sessions_index: dict[str, Session],
        repo: SessionRepository,
    ) -> None:
        self._sessions = sessions_index
        self._repo = repo

    def execute(self) -> list[SessionSummary]:
        summaries: dict[str, SessionSummary] = {
            sid: SessionSummary(session_id=sid, status=s.status)
            for sid, s in self._sessions.items()
        }
        for sid in self._repo.list_session_ids():
            if sid in summaries:
                continue
            session = rehydrate_from_events(sid, self._repo.read_events(sid))
            summaries[sid] = SessionSummary(session_id=sid, status=session.status)
        return sorted(summaries.values(), key=lambda s: s.session_id)
