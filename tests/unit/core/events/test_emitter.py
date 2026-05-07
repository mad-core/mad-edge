"""Unit tests for EventEmitter.

EventEmitter is the single write gateway for the session log.
Every event MUST be persisted before it is published.

Fakes (``FakeEventStore``, ``RecordingEventBus``) live in
``tests/support/events.py`` per heuristic rule 3.
"""

from __future__ import annotations

import pytest

from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from support.events import FakeEventStore, RecordingEventBus


async def test_emit_calls_store_append_once():
    """emit() must call store.append exactly once with the same args."""
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)

    await emitter.emit("sesn_abc", "session.created", {"agent": "claude_cli"})

    assert len(store.calls) == 1
    sid, typ, data = store.calls[0]
    assert sid == "sesn_abc"
    assert typ == "session.created"
    assert data == {"agent": "claude_cli"}


async def test_emit_calls_bus_publish_once_with_store_event():
    """emit() must call bus.publish exactly once with the Event returned by the store."""
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)

    await emitter.emit("sesn_abc", "agent.output", {"line": "hello"})

    assert len(bus.published) == 1
    assert bus.published[0].session_id == "sesn_abc"
    assert bus.published[0].type == "agent.output"
    assert bus.published[0].data == {"line": "hello"}


async def test_emit_returns_the_event_from_store():
    """emit() must return the Event returned by store.append."""
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)

    result = await emitter.emit("sesn_xyz", "user.message", {"content": "hi"})

    assert isinstance(result, Event)
    assert result.session_id == "sesn_xyz"
    assert result.type == "user.message"


async def test_emit_does_not_publish_when_store_raises():
    """If store.append raises, bus.publish must NOT be called (persist first)."""
    store = FakeEventStore(raise_on_append=RuntimeError("disk full"))
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)

    with pytest.raises(RuntimeError, match="disk full"):
        await emitter.emit("sesn_err", "session.error", {"error": "boom"})

    assert len(bus.published) == 0, "bus.publish must not be called when store.append raises"
