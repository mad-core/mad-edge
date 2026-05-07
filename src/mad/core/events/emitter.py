"""EventEmitter — the single write gateway for the session event log.

Every event written to the log MUST go through ``emit()`` (CLAUDE.md
hard rule 9). Use cases receive an EventEmitter as an injected dependency
and never call EventStore.append or EventBus.publish directly.
"""

from __future__ import annotations

from typing import Any

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_store import EventStore


class EventEmitter:
    """Persist an event then publish it to live subscribers."""

    def __init__(self, store: EventStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    async def emit(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        event = self._store.append(session_id, type, data)
        await self._bus.publish(event)
        return event
