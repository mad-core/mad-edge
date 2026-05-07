"""Test-only ``EventBus`` and ``EventLogQuery`` doubles.

Live under ``tests/`` per ADR-0003 — production code in ``src/`` does
not carry fakes. Use cases inject these to verify orchestration
without touching the real asyncio fanout or filesystem walk.
"""

from __future__ import annotations

import asyncio
import datetime
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from mad.core.events.domain.event import Event, event_from_persisted
from mad.core.events.ports.event_bus import EventFilter
from mad.core.events.ports.event_log_query import EventQuery

_NULL_UUID = UUID("00000000-0000-0000-0000-000000000000")
_EPOCH = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)


class FakeEventStore:
    """In-memory ``EventStore`` double.

    Records every ``append`` call and returns a typed ``Event``. Use
    cases drive the emitter through this store; tests then assert on
    ``calls`` (raw tuples) or ``events`` (typed events).
    """

    def __init__(self, *, raise_on_append: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.events: list[Event] = []
        self._raise = raise_on_append

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        if self._raise is not None:
            raise self._raise
        self.calls.append((session_id, type, data))
        event = Event(
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
            session_id=session_id,
            type=type,
            data=data or {},
            timestamp=_EPOCH,
        )
        self.events.append(event)
        return event


class RecordingEventBus:
    """Simple ``EventBus`` double that only records publishes.

    Use this when a test does not need ``subscribe`` (the asyncio-fanout
    semantics live in ``FakeEventBus``).
    """

    def __init__(self) -> None:
        self.published: list[Event] = []

    async def publish(self, event: Event) -> None:
        self.published.append(event)

    def subscribe(self, event_filter: EventFilter) -> AsyncIterator[Event]:  # pragma: no cover
        raise NotImplementedError


class DualInterfaceEventStore:
    """``EventStore`` + legacy ``SessionRepository.append_event`` double.

    Several existing use cases still consult ``read_events`` / ``exists``
    on the session repo while writing through the emitter. This double
    satisfies both interfaces: it persists raw dicts (so legacy reads
    work) and returns a typed ``Event`` (so ``EventEmitter.emit`` is
    happy).
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def append_event(
        self, session_id: str, event_type: str, data: dict | None = None
    ) -> dict:
        event = {"type": event_type, "session_id": session_id, **(data or {})}
        self.events.append(event)
        return event

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        self.append_event(session_id, type, data)
        return Event(
            event_id=_NULL_UUID,
            session_id=session_id,
            type=type,
            data=data or {},
            timestamp=_EPOCH,
        )

    def read_events(self, session_id: str) -> list[dict]:
        return [e for e in self.events if e.get("session_id") == session_id]

    def exists(self, session_id: str) -> bool:
        return any(e.get("session_id") == session_id for e in self.events)

    def list_session_ids(self) -> list[str]:
        return sorted({e["session_id"] for e in self.events if "session_id" in e})


class PersistedEventStore:
    """``EventStore`` double that returns events through ``event_from_persisted``.

    Used by the delete-session test where the session log already has a
    canonical ``event_id`` shape.
    """

    def __init__(self) -> None:
        self.appended: list[tuple[str, str, dict | None]] = []

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        self.appended.append((session_id, type, data))
        raw = {"event_id": None, "type": type, "timestamp": "", **(data or {})}
        return event_from_persisted(raw, session_id)


class FakeEventBus:
    """Records every published event and supports a single async iterator
    subscription per filter. Pre-subscribe publishes are buffered and
    drained on subscribe — that lets test ordering not depend on
    whether the consumer task has been scheduled yet, which keeps
    use-case tests readable. The real asyncio-fanout adapter is
    exercised in ``tests/integration/adapters/events/``.
    """

    def __init__(self) -> None:
        self.published: list[Event] = []
        self._pending: list[Event] = []
        self._subscriber_queue: asyncio.Queue[Event | None] | None = None
        self._subscriber_filter: EventFilter | None = None

    async def publish(self, event: Event) -> None:
        self.published.append(event)
        if self._subscriber_queue is None or self._subscriber_filter is None:
            self._pending.append(event)
            return
        if _matches(event, self._subscriber_filter):
            await self._subscriber_queue.put(event)

    def subscribe(self, event_filter: EventFilter) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subscriber_queue = queue
        self._subscriber_filter = event_filter
        for event in self._pending:
            if _matches(event, event_filter):
                queue.put_nowait(event)
        self._pending.clear()
        return _drain(queue)

    async def close_subscriber(self) -> None:
        """Signal the active subscription to stop iterating."""
        if self._subscriber_queue is not None:
            await self._subscriber_queue.put(None)


async def _drain(queue: asyncio.Queue[Event | None]) -> AsyncIterator[Event]:
    while True:
        item = await queue.get()
        if item is None:
            return
        yield item


def _matches(event: Event, event_filter: EventFilter) -> bool:
    if event_filter.session_id is not None and event.session_id != event_filter.session_id:
        return False
    if event_filter.kind is not None and event.type != event_filter.kind:
        return False
    return not (
        event_filter.session_ids_for_agent is not None
        and event.session_id not in event_filter.session_ids_for_agent
    )


class FakeEventLogQuery:
    """In-memory ``EventLogQuery`` double. Tests script the available
    events and the agent → session_id resolution.
    """

    def __init__(
        self,
        events: list[Event] | None = None,
        agents_to_sessions: dict[str, frozenset[str]] | None = None,
    ) -> None:
        self.events: list[Event] = list(events) if events is not None else []
        self._agents_to_sessions = agents_to_sessions or {}
        self.queries: list[EventQuery] = []

    def query(self, q: EventQuery) -> list[Event]:
        self.queries.append(q)
        result = [e for e in self.events if _matches_query(e, q)]
        result.sort(key=_event_sort_key)
        return result[: q.limit]

    def session_ids_for_agent(self, agent_name: str) -> frozenset[str]:
        return self._agents_to_sessions.get(agent_name, frozenset())


def _matches_query(event: Event, q: EventQuery) -> bool:
    if q.session_id is not None and event.session_id != q.session_id:
        return False
    if q.kind is not None and event.type != q.kind:
        return False
    if q.session_ids_for_agent is not None and event.session_id not in q.session_ids_for_agent:
        return False
    if q.since is not None and event.timestamp < q.since:
        return False
    return not (
        q.after_event_id is not None
        and (event.event_id is None or event.event_id <= q.after_event_id)
    )


def _event_sort_key(event: Event) -> tuple[str, object]:
    eid_str = str(event.event_id) if event.event_id is not None else ""
    return (eid_str, event.timestamp)
