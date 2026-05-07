"""Integration tests for ``InMemoryEventBus``.

Exercises the asyncio fanout and slow-subscriber disconnect policy
documented in ADR-0004. Filter-matching coverage is intentionally
behavior-rich (one test per filter dimension and one for AND combination)
rather than enumerating every branch of ``_matches``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.core.events.domain.event import Event
from mad.core.events.domain.event_id import new_event_id
from mad.core.events.ports.event_bus import EventFilter


def _event(
    *,
    session_id: str = "sesn_a",
    type: str = "agent.output",
    data: dict | None = None,
    event_id: UUID | None = None,
) -> Event:
    return Event(
        event_id=event_id if event_id is not None else new_event_id(),
        session_id=session_id,
        type=type,
        data=data or {},
        timestamp=datetime.now(UTC),
    )


async def _take(stream, n: int, deadline: float = 0.5) -> list[Event]:
    received: list[Event] = []
    async with asyncio.timeout(deadline):
        async for event in stream:
            received.append(event)
            if len(received) == n:
                return received
    return received


async def test_single_subscriber_receives_published_events() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter())

    e1, e2 = _event(), _event()
    await bus.publish(e1)
    await bus.publish(e2)

    received = await _take(stream, 2)
    assert received == [e1, e2]


async def test_two_subscribers_each_get_their_own_copy() -> None:
    bus = InMemoryEventBus()
    a = bus.subscribe(EventFilter())
    b = bus.subscribe(EventFilter())

    event = _event()
    await bus.publish(event)

    received_a, received_b = await asyncio.gather(_take(a, 1), _take(b, 1))
    assert received_a == [event]
    assert received_b == [event]


async def test_filter_by_session_id_narrows_delivery() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter(session_id="sesn_match"))

    await bus.publish(_event(session_id="sesn_other"))
    matching = _event(session_id="sesn_match")
    await bus.publish(matching)

    received = await _take(stream, 1)
    assert received == [matching]


async def test_filter_by_kind_narrows_delivery() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter(kind="session.status_idle"))

    await bus.publish(_event(type="agent.output"))
    idle = _event(type="session.status_idle")
    await bus.publish(idle)

    received = await _take(stream, 1)
    assert received == [idle]


async def test_filter_by_agent_resolved_session_ids() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter(session_ids_for_agent=frozenset({"sesn_a", "sesn_b"})))

    await bus.publish(_event(session_id="sesn_other"))
    a = _event(session_id="sesn_a")
    b = _event(session_id="sesn_b")
    await bus.publish(a)
    await bus.publish(b)

    received = await _take(stream, 2)
    assert received == [a, b]


async def test_filters_are_and_combined() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter(session_id="sesn_a", kind="agent.output"))

    # Each fails one of the two filters.
    await bus.publish(_event(session_id="sesn_b", type="agent.output"))
    await bus.publish(_event(session_id="sesn_a", type="session.status_idle"))
    matching = _event(session_id="sesn_a", type="agent.output")
    await bus.publish(matching)

    received = await _take(stream, 1)
    assert received == [matching]


async def test_slow_subscriber_disconnects_when_queue_fills() -> None:
    """Per ADR-0004 — when a subscriber's bounded queue overflows, the
    bus disconnects it. The publisher is never blocked by the slow
    consumer."""
    bus = InMemoryEventBus(max_queue_size=2)
    stream = bus.subscribe(EventFilter())

    # Publish 5 without anyone consuming. The queue size is 2 plus the
    # _CLOSE sentinel; subsequent publishes drop the subscriber.
    for _ in range(5):
        await bus.publish(_event())

    # The subscriber should now drain its 2 buffered events and stop
    # cleanly when it hits the close sentinel.
    received: list[Event] = []
    async with asyncio.timeout(0.5):
        async for event in stream:
            received.append(event)

    assert len(received) == 2

    # And the bus should no longer carry that subscriber.
    sentinel = _event(type="post-disconnect")
    await bus.publish(sentinel)  # must not raise
    # New subscriber should still work — bus itself is healthy.
    fresh = bus.subscribe(EventFilter())
    final = _event(type="final")
    await bus.publish(final)
    assert (await _take(fresh, 1)) == [final]


async def test_subscriber_removed_after_consumer_exits() -> None:
    bus = InMemoryEventBus()
    stream = bus.subscribe(EventFilter())

    await bus.publish(_event())
    received = await _take(stream, 1)
    assert len(received) == 1
    await stream.aclose()

    # Publishing now should not deliver to the closed iterator and must
    # not raise.
    await bus.publish(_event())


@pytest.fixture
def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop()
