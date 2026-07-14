"""Unit tests for ``InMemoryTaskProjection._on_queued`` auto_sync readback (#109).

The dispatcher resolves the post-run auto-sync gate from ``Task.auto_sync``, and
the only way a ``Task`` gets one is the projection reading it off the
``task.queued`` event. Two properties matter:

* **Only a literal bool overrides.** ``None``/absent means "no per-task override"
  so the dispatcher falls back to session > env > ``True``. A pre-#109
  ``task.queued`` event has no ``auto_sync`` key at all and MUST keep replaying
  with the safety net on.
* **No truthiness coercion.** A junk value (say the string ``"false"``, which is
  truthy in Python) must not be mistaken for an override in either direction.

Builds ``Event`` objects directly and drives ``apply``, mirroring the helper
style of ``tests/unit/adapters/outbound/orchestration/test_projection_deferred.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.domain.event import Event

_QUEUED_AT = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
_SESSION_ID = "sesn_q"


def _queued_event(task_id: UUID, data: dict[str, Any]) -> Event:
    return Event(
        event_id=uuid4(),
        session_id=_SESSION_ID,
        type="task.queued",
        data={
            "task_id": str(task_id),
            "content": "do the thing",
            "scheduled_for": "now",
            **data,
        },
        timestamp=_QUEUED_AT,
    )


def _queue_and_read(data: dict[str, Any]) -> Any:
    projection = InMemoryTaskProjection()
    task_id = uuid4()
    projection.apply(_queued_event(task_id, data))
    queued = projection.queued(_SESSION_ID)
    assert len(queued) == 1, f"expected exactly one queued task, got {queued}"
    assert queued[0].task_id == task_id
    return queued[0].auto_sync


def test_queued_task_carries_auto_sync_false() -> None:
    """A task that manages its own branch/PR enqueues with ``auto_sync=False``;
    the projection hands that to the dispatcher verbatim."""
    assert _queue_and_read({"auto_sync": False}) is False


def test_queued_task_carries_auto_sync_true() -> None:
    """Negative twin: an explicit opt-IN reads back as True, so the False above is
    a real read and not a default."""
    assert _queue_and_read({"auto_sync": True}) is True


def test_queued_task_auto_sync_is_none_when_key_absent() -> None:
    """Negative twin: a pre-#109 ``task.queued`` event has no ``auto_sync`` key.
    It must replay as ``None`` (inherit session > env > True), never as False —
    that would retroactively disable auto-sync for every historical task."""
    assert _queue_and_read({}) is None


def test_queued_task_auto_sync_is_none_when_explicitly_null() -> None:
    """The default enqueue path persists ``auto_sync: null``; that is "inherit"."""
    assert _queue_and_read({"auto_sync": None}) is None


def test_queued_task_auto_sync_ignores_a_non_boolean_value() -> None:
    """Negative twin: a non-bool payload (a malformed producer, a hand-edited log)
    is NOT truthiness-coerced. The string ``"false"`` is truthy in Python — reading
    it as an override in either direction would be a silent, wrong decision, so
    the projection discards it and falls back to inherit."""
    assert _queue_and_read({"auto_sync": "false"}) is None
    assert _queue_and_read({"auto_sync": 1}) is None
