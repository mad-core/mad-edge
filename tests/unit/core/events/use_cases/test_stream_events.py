"""Unit tests for ``StreamEventsUseCase``.

Covers the orchestration only — the asyncio fanout adapter and the
JSONL query adapter have their own integration tests. These tests use
the ``FakeEventBus`` and ``FakeEventLogQuery`` doubles from
``tests/support/events.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from mad.core.events.domain.event import Event
from mad.core.events.domain.event_id import new_event_id
from mad.core.events.use_cases.stream_events import (
    StreamEventsInput,
    StreamEventsUseCase,
)
from support.events import FakeEventBus, FakeEventLogQuery


def _eid(seq: int) -> UUID:
    """Deterministic UUIDv7-shaped event_id for tests where mint order
    matters. Within a single millisecond real UUIDv7 ids sort randomly
    (ADR-0005); this helper sets the millisecond prefix to ``seq`` so
    lex-sort matches input order.
    """
    return UUID(int=(seq << 80) | (0x7 << 76) | (0b10 << 62))


def _event(
    *,
    session_id: str = "sesn_a",
    type: str = "agent.output",
    event_id=None,
    data: dict | None = None,
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


async def test_streams_live_events_when_no_last_event_id() -> None:
    bus = FakeEventBus()
    log = FakeEventLogQuery()
    use_case = StreamEventsUseCase(bus=bus, log=log)

    stream = use_case.execute(StreamEventsInput())
    e1 = _event()
    e2 = _event()
    await bus.publish(e1)
    await bus.publish(e2)

    received = await _take(stream, 2)

    assert received == [e1, e2]
    assert log.queries == []  # no replay


async def test_replay_then_live_with_last_event_id() -> None:
    bus = FakeEventBus()
    e1 = _event(event_id=_eid(1), data={"line": "1"})
    e2 = _event(event_id=_eid(2), data={"line": "2"})
    e3 = _event(event_id=_eid(3), data={"line": "3"})
    log = FakeEventLogQuery(events=[e1, e2, e3])
    use_case = StreamEventsUseCase(bus=bus, log=log)

    # Resume after e1 — replay should yield e2, e3 in that order.
    stream = use_case.execute(StreamEventsInput(last_event_id=e1.event_id))

    replayed = await _take(stream, 2)
    assert replayed == [e2, e3]

    # Now live events flow in.
    e4 = _event(event_id=_eid(4), data={"line": "4"})
    await bus.publish(e4)
    live = await _take(stream, 1)
    assert live == [e4]


async def test_dedupes_event_appearing_in_replay_and_live() -> None:
    """The writer persists then publishes; if the replay query reads the
    just-written event and the bus also delivers it live, the use case
    must yield it exactly once (dedup boundary fixed at end-of-replay)."""
    bus = FakeEventBus()
    e1 = _event(event_id=_eid(1), data={"line": "1"})
    e2 = _event(event_id=_eid(2), data={"line": "2"})
    e3 = _event(event_id=_eid(3), data={"line": "3"})
    log = FakeEventLogQuery(events=[e1, e2])
    use_case = StreamEventsUseCase(bus=bus, log=log)

    stream = use_case.execute(StreamEventsInput(last_event_id=e1.event_id))

    # Replay yields e2; the writer also publishes e2 live (writer order).
    replayed = await _take(stream, 1)
    assert replayed == [e2]

    await bus.publish(e2)  # would be a duplicate of the replayed e2
    await bus.publish(e3)  # genuinely new

    live = await _take(stream, 1)
    assert live == [e3]  # e2 was suppressed


async def test_agent_filter_resolves_session_ids_before_subscribing() -> None:
    bus = FakeEventBus()
    log = FakeEventLogQuery(
        agents_to_sessions={"claude_cli": frozenset({"sesn_a"})},
    )
    use_case = StreamEventsUseCase(bus=bus, log=log)

    stream = use_case.execute(StreamEventsInput(agent="claude_cli"))

    # Publish from non-matching session — must not be delivered.
    await bus.publish(_event(session_id="sesn_other"))
    matching = _event(session_id="sesn_a")
    await bus.publish(matching)

    received = await _take(stream, 1)
    assert received == [matching]


async def test_session_id_and_kind_filters_passed_to_bus() -> None:
    bus = FakeEventBus()
    log = FakeEventLogQuery()
    use_case = StreamEventsUseCase(bus=bus, log=log)

    stream = use_case.execute(StreamEventsInput(session_id="sesn_a", kind="agent.output"))

    await bus.publish(_event(session_id="sesn_b", type="agent.output"))
    await bus.publish(_event(session_id="sesn_a", type="session.status_idle"))
    matching = _event(session_id="sesn_a", type="agent.output")
    await bus.publish(matching)

    received = await _take(stream, 1)
    assert received == [matching]


@pytest.fixture
def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    return asyncio.new_event_loop()
