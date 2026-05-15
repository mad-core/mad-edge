"""Unit tests for ``mad.core.orchestration.domain.task.Task``.

Verifies the entity is a frozen, value-equal record. The fields it
carries are load-bearing for the projection (ADR-0009 Decision 3) and
for the ``task.queued`` event payload, so changes to its shape are
observable.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from mad.core.orchestration.domain.task import Task


def _sample(**overrides: object) -> Task:
    base: dict[str, object] = {
        "task_id": uuid4(),
        "session_id": "sesn_abc",
        "content": "fix issue #42",
        "scheduled_for": "now",
        "created_at": datetime(2026, 5, 8, 18, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Task(**base)  # type: ignore[arg-type]


def test_task_is_frozen() -> None:
    task = _sample()
    with pytest.raises(FrozenInstanceError):
        task.session_id = "other"  # type: ignore[misc]


def test_equal_when_all_fields_match() -> None:
    task_id = uuid4()
    created = datetime(2026, 5, 8, 18, 0, tzinfo=UTC)
    a = _sample(task_id=task_id, created_at=created)
    b = _sample(task_id=task_id, created_at=created)
    assert a == b


def test_unequal_when_a_field_differs() -> None:
    task_id = uuid4()
    created = datetime(2026, 5, 8, 18, 0, tzinfo=UTC)
    a = _sample(task_id=task_id, created_at=created, content="A")
    b = _sample(task_id=task_id, created_at=created, content="B")
    assert a != b


def test_preserves_each_field_verbatim() -> None:
    task_id = uuid4()
    created = datetime(2026, 5, 8, 18, 0, tzinfo=UTC)
    task = Task(
        task_id=task_id,
        session_id="sesn_xyz",
        content="opaque content the module never parses",
        scheduled_for="next_window",
        created_at=created,
    )

    assert task.task_id == task_id
    assert isinstance(task.task_id, UUID)
    assert task.session_id == "sesn_xyz"
    assert task.content == "opaque content the module never parses"
    assert task.scheduled_for == "next_window"
    assert task.created_at == created
    assert task.created_at.tzinfo is UTC
