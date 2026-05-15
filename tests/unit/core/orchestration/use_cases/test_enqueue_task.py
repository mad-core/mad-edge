"""Unit tests for ``EnqueueTaskUseCase``.

Covers the happy path (the single emit call with the task.queued
payload) and the negative twin (unknown session). Heuristic 1 — every
endpoint test has a negative twin.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.events import FakeEventStore, RecordingEventBus


def _session(session_id: str = "sesn_a") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test-agent", "provider": "fake"},
        workspace="/tmp/mad_test",
        tokens_to_redact=[],
    )


def _make_use_case(
    sessions: dict[str, Session] | None = None,
) -> tuple[EnqueueTaskUseCase, FakeEventStore, RecordingEventBus]:
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    use_case = EnqueueTaskUseCase(
        sessions_index=sessions if sessions is not None else {"sesn_a": _session()},
        emitter=emitter,
    )
    return use_case, store, bus


async def test_enqueues_task_emits_task_queued_with_full_payload() -> None:
    use_case, store, bus = _make_use_case()

    output = await use_case.execute(EnqueueTaskInput(session_id="sesn_a", content="fix issue #42"))

    assert isinstance(output.task_id, UUID)
    assert output.session_id == "sesn_a"
    assert output.scheduled_for == "now"

    # Exactly one event was persisted: the task.queued.
    assert len(store.calls) == 1
    session_id, event_type, data = store.calls[0]
    assert session_id == "sesn_a"
    assert event_type == "task.queued"
    assert data == {
        "task_id": str(output.task_id),
        "content": "fix issue #42",
        "scheduled_for": "now",
    }

    # And it was published on the bus exactly once.
    assert len(bus.published) == 1
    assert bus.published[0].type == "task.queued"
    assert bus.published[0].data["task_id"] == str(output.task_id)


async def test_passes_through_explicit_scheduled_for() -> None:
    use_case, store, _ = _make_use_case()

    output = await use_case.execute(
        EnqueueTaskInput(session_id="sesn_a", content="overnight job", scheduled_for="next_window")
    )

    assert output.scheduled_for == "next_window"
    assert store.calls[-1][2] == {
        "task_id": str(output.task_id),
        "content": "overnight job",
        "scheduled_for": "next_window",
    }


async def test_rejects_unknown_session_with_session_not_found() -> None:
    use_case, store, bus = _make_use_case(sessions={})

    with pytest.raises(SessionNotFound):
        await use_case.execute(EnqueueTaskInput(session_id="sesn_missing", content="anything"))

    # Nothing persisted, nothing published.
    assert store.calls == []
    assert bus.published == []


async def test_each_call_mints_a_distinct_task_id() -> None:
    use_case, _, _ = _make_use_case()

    a = await use_case.execute(EnqueueTaskInput(session_id="sesn_a", content="A"))
    b = await use_case.execute(EnqueueTaskInput(session_id="sesn_a", content="B"))

    assert a.task_id != b.task_id
