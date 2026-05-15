"""Unit tests for ``CancelTaskUseCase``.

Covers the happy path (a queued task is cancelled and ``task.cancelled``
is emitted) and the three negative twins (unknown session, unknown
task, in-flight task) — each maps to a different HTTP status code per
ADR-0009 Decision 6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.exceptions.base import (
    TaskAlreadyDispatched,
    TaskNotFound,
)
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.cancel_task import (
    CancelTaskInput,
    CancelTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.events import FakeEventStore, RecordingEventBus
from support.orchestration import FakeTaskQueue


def _session(session_id: str = "sesn_a") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test-agent", "provider": "fake"},
        workspace="/tmp/mad_test",
        tokens_to_redact=[],
    )


def _task(task_id: UUID, session_id: str = "sesn_a") -> Task:
    return Task(
        task_id=task_id,
        session_id=session_id,
        content="opaque",
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


def _make_use_case(
    *,
    sessions: dict[str, Session] | None = None,
    queue: FakeTaskQueue | None = None,
) -> tuple[CancelTaskUseCase, FakeEventStore, RecordingEventBus]:
    store = FakeEventStore()
    bus = RecordingEventBus()
    emitter = EventEmitter(store=store, bus=bus)
    use_case = CancelTaskUseCase(
        sessions_index=sessions if sessions is not None else {"sesn_a": _session()},
        task_queue=queue if queue is not None else FakeTaskQueue(),
        emitter=emitter,
    )
    return use_case, store, bus


async def test_cancels_queued_task_emits_task_cancelled() -> None:
    task_id = uuid4()
    queue = FakeTaskQueue(queued={"sesn_a": [_task(task_id)]})
    use_case, store, bus = _make_use_case(queue=queue)

    await use_case.execute(
        CancelTaskInput(session_id="sesn_a", task_id=task_id, reason="user_cancelled")
    )

    assert len(store.calls) == 1
    session_id, event_type, data = store.calls[0]
    assert session_id == "sesn_a"
    assert event_type == "task.cancelled"
    assert data == {"task_id": str(task_id), "reason": "user_cancelled"}

    assert len(bus.published) == 1
    assert bus.published[0].type == "task.cancelled"


async def test_passes_explicit_reason_through_to_event_payload() -> None:
    task_id = uuid4()
    queue = FakeTaskQueue(queued={"sesn_a": [_task(task_id)]})
    use_case, store, _ = _make_use_case(queue=queue)

    await use_case.execute(
        CancelTaskInput(session_id="sesn_a", task_id=task_id, reason="superseded")
    )

    assert store.calls[-1][2] == {"task_id": str(task_id), "reason": "superseded"}


async def test_rejects_unknown_session_with_session_not_found() -> None:
    use_case, store, bus = _make_use_case(sessions={})

    with pytest.raises(SessionNotFound):
        await use_case.execute(CancelTaskInput(session_id="sesn_missing", task_id=uuid4()))

    assert store.calls == []
    assert bus.published == []


async def test_rejects_unknown_task_with_task_not_found() -> None:
    queue = FakeTaskQueue(queued={"sesn_a": []})
    use_case, store, bus = _make_use_case(queue=queue)
    unknown_id = uuid4()

    with pytest.raises(TaskNotFound) as excinfo:
        await use_case.execute(CancelTaskInput(session_id="sesn_a", task_id=unknown_id))

    assert excinfo.value.task_id == unknown_id
    assert store.calls == []
    assert bus.published == []


async def test_rejects_in_flight_task_with_task_already_dispatched() -> None:
    task_id = uuid4()
    queue = FakeTaskQueue(in_flight={"sesn_a": _task(task_id)})
    use_case, store, bus = _make_use_case(queue=queue)

    with pytest.raises(TaskAlreadyDispatched) as excinfo:
        await use_case.execute(CancelTaskInput(session_id="sesn_a", task_id=task_id))

    assert excinfo.value.task_id == task_id
    assert store.calls == []
    assert bus.published == []
