"""Unit tests for ``UpdateDispatchPriorityUseCase`` (issue #46 Part B).

Covers the happy path (priority set on the session + the durable
``dispatch_priority.updated`` event emitted through the single write
gateway), the unknown-session twin, and the out-of-range twin (rejected
loudly, never clamped, and never persisted).
"""

from __future__ import annotations

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.ordering import InvalidPriority
from mad.core.orchestration.use_cases.update_dispatch_priority import (
    UpdateDispatchPriorityInput,
    UpdateDispatchPriorityUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.events import FakeEventStore, RecordingEventBus


def _session(session_id: str = "sesn_a") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_t",
        tokens_to_redact=[],
    )


def _make_use_case(
    sessions: dict[str, Session],
) -> tuple[UpdateDispatchPriorityUseCase, FakeEventStore]:
    store = FakeEventStore()
    emitter = EventEmitter(store=store, bus=RecordingEventBus())
    return UpdateDispatchPriorityUseCase(sessions_index=sessions, emitter=emitter), store


async def test_update_sets_priority_and_emits_event() -> None:
    sessions = {"sesn_a": _session()}
    use_case, store = _make_use_case(sessions)

    output = await use_case.execute(UpdateDispatchPriorityInput(session_id="sesn_a", priority=8))

    assert output.priority == 8
    assert sessions["sesn_a"].priority == 8
    assert store.calls == [("sesn_a", "dispatch_priority.updated", {"priority": 8})]


async def test_update_unknown_session_raises_session_not_found() -> None:
    use_case, store = _make_use_case(sessions={})

    with pytest.raises(SessionNotFound):
        await use_case.execute(UpdateDispatchPriorityInput(session_id="sesn_missing", priority=5))
    assert store.calls == []


async def test_update_out_of_range_priority_is_rejected_and_not_persisted() -> None:
    """Negative twin: an out-of-range value must raise AND leave both the
    session and the event log untouched — a clamped or half-applied write
    would be unreplayable."""
    session = _session()
    sessions = {"sesn_a": session}
    use_case, store = _make_use_case(sessions)

    with pytest.raises(InvalidPriority):
        await use_case.execute(UpdateDispatchPriorityInput(session_id="sesn_a", priority=11))

    assert session.priority == 1
    assert store.calls == []
