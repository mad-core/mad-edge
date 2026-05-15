"""Unit tests for ``ListTasksUseCase``.

Thin wrapper over the ``TaskQueue`` port; the projection's behaviour
is exercised in ``tests/integration/adapters/orchestration/test_projection.py``.
Here we pin the use case's session-validation semantics and the
output shape (heuristics 1 + 4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.list_tasks import ListTasksUseCase
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from support.orchestration import FakeTaskQueue


def _session(session_id: str = "sesn_a") -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test-agent", "provider": "fake"},
        workspace="/tmp/mad_test",
        tokens_to_redact=[],
    )


def _task(task_id: UUID | None = None) -> Task:
    return Task(
        task_id=task_id if task_id is not None else uuid4(),
        session_id="sesn_a",
        content="opaque",
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )


def test_returns_queued_and_in_flight_for_known_session() -> None:
    queued_task = _task()
    in_flight_task = _task()
    queue = FakeTaskQueue(
        queued={"sesn_a": [queued_task]},
        in_flight={"sesn_a": in_flight_task},
    )
    use_case = ListTasksUseCase(
        sessions_index={"sesn_a": _session()},
        task_queue=queue,
    )

    output = use_case.execute("sesn_a")

    assert output.queued == [queued_task]
    assert output.in_flight == in_flight_task


def test_returns_empty_lists_when_session_has_no_tasks() -> None:
    use_case = ListTasksUseCase(
        sessions_index={"sesn_a": _session()},
        task_queue=FakeTaskQueue(),
    )

    output = use_case.execute("sesn_a")

    assert output.queued == []
    assert output.in_flight is None


def test_rejects_unknown_session_with_session_not_found() -> None:
    use_case = ListTasksUseCase(
        sessions_index={},
        task_queue=FakeTaskQueue(),
    )

    with pytest.raises(SessionNotFound):
        use_case.execute("sesn_missing")
