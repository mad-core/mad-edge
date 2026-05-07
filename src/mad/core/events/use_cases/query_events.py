"""QueryEventsUseCase — paginated historical query.

Resolves ``?agent=<name>`` to a set of session ids, clamps ``limit`` to
``MAX_LIMIT``, and returns a ``next_cursor`` (the ``event_id`` of the
last returned event) when more results are available.

Pre-UUIDv7 events surface with ``event_id=None``; if the last event in
a page has no id the cursor is ``None`` (pagination ends there). Per
ADR-0005 this is acceptable for v1 since legacy events sort first and
age out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_log_query import EventLogQuery, EventQuery


@dataclass(frozen=True)
class QueryEventsInput:
    session_id: str | None = None
    kind: str | None = None
    agent: str | None = None
    since: datetime | None = None
    after_event_id: UUID | None = None
    limit: int = 100


@dataclass(frozen=True)
class QueryEventsOutput:
    events: list[Event]
    next_cursor: UUID | None


class QueryEventsUseCase:
    """Paginated historical event query."""

    DEFAULT_LIMIT = 100
    MAX_LIMIT = 1000

    def __init__(self, log: EventLogQuery) -> None:
        self._log = log

    def execute(self, payload: QueryEventsInput) -> QueryEventsOutput:
        agent_sessions = (
            self._log.session_ids_for_agent(payload.agent) if payload.agent is not None else None
        )

        effective_limit = max(1, min(payload.limit, self.MAX_LIMIT))

        # Fetch one extra to detect "more available" without a separate
        # COUNT path.
        events = list(
            self._log.query(
                EventQuery(
                    session_id=payload.session_id,
                    kind=payload.kind,
                    session_ids_for_agent=agent_sessions,
                    since=payload.since,
                    after_event_id=payload.after_event_id,
                    limit=effective_limit + 1,
                )
            )
        )

        if len(events) > effective_limit:
            page = events[:effective_limit]
            next_cursor = page[-1].event_id
        else:
            page = events
            next_cursor = None

        return QueryEventsOutput(events=page, next_cursor=next_cursor)
