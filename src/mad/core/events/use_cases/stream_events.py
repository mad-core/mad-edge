"""StreamEventsUseCase — filtered live tail with Last-Event-ID catch-up.

Subscribes to the ``EventBus`` first so no live event is missed while
catch-up replay runs. After replay, drains live events and skips any
whose ``event_id`` is at or below the last replayed id (the same event
appears in both the log and the live stream because the writer
publishes after persisting).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_bus import EventBus, EventFilter
from mad.core.events.ports.event_log_query import EventLogQuery, EventQuery

_REPLAY_PAGE_SIZE = 1000


@dataclass(frozen=True)
class StreamEventsInput:
    """Filter spec + optional ``Last-Event-ID`` for SSE reconnect."""

    session_id: str | None = None
    kind: str | None = None
    agent: str | None = None
    last_event_id: UUID | None = None


class StreamEventsUseCase:
    """Filtered live event stream with replay catch-up."""

    def __init__(self, bus: EventBus, log: EventLogQuery) -> None:
        self._bus = bus
        self._log = log

    async def execute(self, payload: StreamEventsInput) -> AsyncIterator[Event]:
        session_ids_for_agent = (
            self._log.session_ids_for_agent(payload.agent) if payload.agent is not None else None
        )

        bus_filter = EventFilter(
            session_id=payload.session_id,
            kind=payload.kind,
            session_ids_for_agent=session_ids_for_agent,
        )
        # Subscribe BEFORE replay so live events that arrive during
        # replay are buffered, not lost.
        live = self._bus.subscribe(bus_filter)

        # ``dedup_until`` is the boundary set at end-of-replay. Live
        # events with an event_id at or below this boundary are
        # duplicates of events the writer pushed to both the log and
        # the bus during the replay window. Within-millisecond UUIDv7
        # ordering is random (ADR-0005), so we deliberately do NOT
        # advance the boundary from live events themselves.
        dedup_until = payload.last_event_id

        if payload.last_event_id is not None:
            async for event in self._replay(payload, session_ids_for_agent, payload.last_event_id):
                yield event
                if event.event_id is not None:
                    dedup_until = event.event_id

        async for event in live:
            if (
                dedup_until is not None
                and event.event_id is not None
                and event.event_id <= dedup_until
            ):
                continue
            yield event

    async def _replay(
        self,
        payload: StreamEventsInput,
        session_ids_for_agent: frozenset[str] | None,
        starting_after: UUID,
    ) -> AsyncIterator[Event]:
        cursor = starting_after
        while True:
            page = self._log.query(
                EventQuery(
                    session_id=payload.session_id,
                    kind=payload.kind,
                    session_ids_for_agent=session_ids_for_agent,
                    after_event_id=cursor,
                    limit=_REPLAY_PAGE_SIZE,
                )
            )
            page_list = list(page)
            for event in page_list:
                yield event
            if len(page_list) < _REPLAY_PAGE_SIZE:
                return
            last_id = page_list[-1].event_id
            if last_id is None:
                # Pre-UUIDv7 events have no cursor; we cannot paginate
                # past them deterministically. Stop replay here; live
                # tail will pick up from where the bus is sitting.
                return
            cursor = last_id
