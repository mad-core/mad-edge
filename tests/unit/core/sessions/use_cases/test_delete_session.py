"""Unit tests for DeleteSessionUseCase."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.task import Task
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.delete_session import DeleteSessionUseCase
from support.events import PersistedEventStore as FakeStore
from support.events import RecordingEventBus as FakeBus
from support.sessions import FakeProvisioner


def _make_session(session_id="sesn_del", status="idle"):
    s = Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_" + session_id,
    )
    s.status = status
    return s


def _queued_task(session_id: str, content: str, minute: int) -> Task:
    return Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 6, 13, 19, minute, tzinfo=UTC),
    )


def _make_uc(sessions, provisioner):
    store = FakeStore()
    bus = FakeBus()
    emitter = EventEmitter(store=store, bus=bus)
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        emitter=emitter,
        task_queue=InMemoryTaskProjection(),
    )
    return uc, store, bus


async def test_delete_session_happy_path():
    sessions = {"sesn_del": _make_session(status="idle")}
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner)
    out = await uc.execute("sesn_del")
    assert out.status == "deleted"
    assert out.session_id == "sesn_del"
    assert sessions["sesn_del"].status == "deleted"
    assert "sesn_del" in provisioner.destroyed


async def test_delete_session_emits_session_deleted_event():
    """Negative twin to ``test_delete_session_cancels_queued_tasks``: with an
    empty queue, deletion emits ONLY ``session.deleted`` — no spurious
    ``task.cancelled`` (exactly one event)."""
    sessions = {"sesn_del": _make_session(status="idle")}
    provisioner = FakeProvisioner()
    uc, store, bus = _make_uc(sessions, provisioner)
    await uc.execute("sesn_del")
    assert store.appended == [("sesn_del", "session.deleted", {"final_status": "idle"})]
    assert len(bus.published) == 1
    assert bus.published[0].type == "session.deleted"
    assert bus.published[0].data == {"final_status": "idle"}


async def test_delete_session_cancels_queued_tasks():
    """A session deleted while tasks are queued cancels each one (reason
    ``session_deleted``) BEFORE ``session.deleted``, so the orphan never
    outlives the session in the cross-session queue (issue #46). Replaying
    the emitted events into the projection drains the session entirely —
    the bug was that nothing did this, leaving the task in ``scheduled``.
    """
    sid = "sesn_del"
    sessions = {sid: _make_session(sid, status="idle")}
    provisioner = FakeProvisioner()
    projection = InMemoryTaskProjection()
    first = _queued_task(sid, "overnight VIP", minute=28)
    second = _queued_task(sid, "follow-up", minute=29)
    projection._queued[sid] = [first, second]

    store = FakeStore()
    bus = FakeBus()
    emitter = EventEmitter(store=store, bus=bus)
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        emitter=emitter,
        task_queue=projection,
    )

    await uc.execute(sid)

    # Both queued tasks cancelled, in queue order, ahead of session.deleted.
    assert [t for _, t, _ in store.appended] == [
        "task.cancelled",
        "task.cancelled",
        "session.deleted",
    ]
    cancelled = [e for e in bus.published if e.type == "task.cancelled"]
    assert [e.data["task_id"] for e in cancelled] == [str(first.task_id), str(second.task_id)]
    assert all(e.data["reason"] == "session_deleted" for e in cancelled)

    # Replaying the emitted events through the real projection removes the
    # session from the queue — the orphan is gone, not merely hidden.
    for event in bus.published:
        projection.apply(event)
    assert projection.queued(sid) == []
    assert sid not in projection.pending_session_ids()


async def test_delete_session_not_found_raises():
    sessions: dict = {}
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner)
    with pytest.raises(SessionNotFound):
        await uc.execute("sesn_missing")
