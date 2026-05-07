"""In-memory ``EventBus`` implementation for single-process Mad.

Each subscriber owns a bounded ``asyncio.Queue``. When the queue fills
(publisher faster than subscriber drains) the bus pushes a sentinel,
disconnects the subscriber, and removes it from the active set. Per
ADR-0004 the SSE client recovers by reconnecting with ``Last-Event-ID``
and replaying through ``EventLogQuery``.

Filter matching is centralized in ``_matches`` so both subscribe-time
admission and per-publish dispatch share one rule. ``EventFilter`` is
AND-combined: each non-``None`` field narrows the matched set.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_bus import EventFilter

_DEFAULT_QUEUE_SIZE = 256
_CLOSE: object = object()


class InMemoryEventBus:
    """Reference ``EventBus`` for single-process deployments."""

    def __init__(self, max_queue_size: int = _DEFAULT_QUEUE_SIZE) -> None:
        self._max_queue_size = max_queue_size
        self._subscribers: list[tuple[EventFilter, asyncio.Queue[Event | object]]] = []

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every matching subscriber.

        Disconnects (and removes) any subscriber whose queue is full.
        """
        # Snapshot the list so we can mutate it as we go.
        for filter_, queue in list(self._subscribers):
            if not _matches(event, filter_):
                continue
            # The queue is sized to ``max_queue_size + 1`` so the +1 slot
            # is always reserved for the disconnect sentinel. Overflow is
            # measured against the user-visible capacity.
            if queue.qsize() >= self._max_queue_size:
                queue.put_nowait(_CLOSE)
                with contextlib.suppress(ValueError):
                    self._subscribers.remove((filter_, queue))
            else:
                queue.put_nowait(event)

    def subscribe(self, event_filter: EventFilter) -> AsyncIterator[Event]:
        """Register a subscriber and return its event stream."""
        # +1 reserves a slot for the disconnect sentinel even when full.
        queue: asyncio.Queue[Event | object] = asyncio.Queue(self._max_queue_size + 1)
        entry = (event_filter, queue)
        self._subscribers.append(entry)
        return self._consume(entry, queue)

    async def _consume(
        self,
        entry: tuple[EventFilter, asyncio.Queue[Event | object]],
        queue: asyncio.Queue[Event | object],
    ) -> AsyncIterator[Event]:
        try:
            while True:
                item = await queue.get()
                if item is _CLOSE:
                    return
                # Only Event instances are pushed besides _CLOSE.
                yield item  # type: ignore[misc]
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(entry)


def _matches(event: Event, event_filter: EventFilter) -> bool:
    if event_filter.session_id is not None and event.session_id != event_filter.session_id:
        return False
    if event_filter.kind is not None and event.type != event_filter.kind:
        return False
    return not (
        event_filter.session_ids_for_agent is not None
        and event.session_id not in event_filter.session_ids_for_agent
    )
