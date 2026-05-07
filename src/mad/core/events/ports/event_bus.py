"""EventBus port — pub/sub for live event delivery to subscribers.

Producers (use cases) publish ``Event`` instances; subscribers consume
matching events via an async iterator. Implementations decide how to
fan out (in-memory queues for v1; Redis Streams or NATS later) but
the contract here does not change.

Slow-subscriber policy is implementation-defined. The reference
``InMemoryEventBus`` disconnects subscribers whose queues fill, on the
expectation that clients reconnect with ``Last-Event-ID`` and catch up
via ``EventLogQuery`` (see ADR-0004).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol

from mad.core.events.domain.event import Event


@dataclass(frozen=True)
class EventFilter:
    """AND-combined filter for stream subscribers.

    Each non-``None`` field narrows the matched set. ``session_id`` and
    ``kind`` filter against the event itself. ``session_ids_for_agent``
    is the resolved set of sessions whose ``session.created`` event
    named the requested agent — resolution lives in the use case so
    the bus stays vocabulary-agnostic (ADR-0004).
    """

    session_id: str | None = None
    kind: str | None = None
    session_ids_for_agent: frozenset[str] | None = None


class EventBus(Protocol):
    """Publish events to live subscribers."""

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to every subscriber whose filter matches."""
        ...

    def subscribe(self, event_filter: EventFilter) -> AsyncIterator[Event]:
        """Open a subscription. Yields matching events until disconnect."""
        ...
