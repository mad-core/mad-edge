"""ListSessions use case."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.rehydrate import rehydrate_from_events
from mad.core.sessions.ports.outbound.session_repository import SessionRepository


@dataclass
class SessionSummary:
    session_id: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime


@dataclass
class ListSessionsInput:
    """Filter and ordering criteria for listing sessions.

    All fields default to "no filter / no ordering preference"; the use
    case always returns the full set ordered by ``session_id`` when no
    ``order_by`` is supplied (stable for clients that still rely on the
    pre-filter contract).
    """

    created_after: datetime | None = None
    created_before: datetime | None = None
    updated_after: datetime | None = None
    updated_before: datetime | None = None
    order_by: Literal["created_at", "updated_at"] | None = None
    order: Literal["asc", "desc"] = "asc"
    include_deleted: bool = False


@dataclass
class ListSessionsOutput:
    sessions: list[SessionSummary] = field(default_factory=list)


class ListSessionsUseCase:
    """Return a summary of every known session, optionally filtered and ordered.

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

    def execute(
        self, payload: ListSessionsInput | None = None
    ) -> ListSessionsOutput:
        criteria = payload or ListSessionsInput()

        summaries: dict[str, SessionSummary] = {
            sid: SessionSummary(
                session_id=sid,
                status=s.status,
                priority=s.priority,
                created_at=s.created_at,
                updated_at=s.updated_at,
            )
            for sid, s in self._sessions.items()
        }
        for sid in self._repo.list_session_ids():
            if sid in summaries:
                continue
            session = rehydrate_from_events(sid, self._repo.read_events(sid))
            summaries[sid] = SessionSummary(
                session_id=sid,
                status=session.status,
                priority=session.priority,
                created_at=session.created_at,
                updated_at=session.updated_at,
            )

        filtered = [s for s in summaries.values() if _matches(s, criteria)]
        filtered.sort(key=_order_key(criteria), reverse=(criteria.order == "desc"))
        return ListSessionsOutput(sessions=filtered)


def _matches(s: SessionSummary, c: ListSessionsInput) -> bool:
    if not c.include_deleted and s.status == "deleted":
        return False
    if c.created_after is not None and s.created_at < c.created_after:
        return False
    if c.created_before is not None and s.created_at > c.created_before:
        return False
    if c.updated_after is not None and s.updated_at < c.updated_after:
        return False
    return not (c.updated_before is not None and s.updated_at > c.updated_before)


def _order_key(
    c: ListSessionsInput,
) -> Callable[[SessionSummary], Any]:
    if c.order_by == "created_at":
        return lambda s: (s.created_at, s.session_id)
    if c.order_by == "updated_at":
        return lambda s: (s.updated_at, s.session_id)
    return lambda s: s.session_id
