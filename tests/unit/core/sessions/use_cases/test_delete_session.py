"""Unit tests for DeleteSessionUseCase."""

from __future__ import annotations

import pytest

from mad.core.events.domain.event import event_from_persisted
from mad.core.events.emitter import EventEmitter
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.delete_session import DeleteSessionUseCase


class FakeProvisioner:
    def __init__(self):
        self.destroyed: list[str] = []

    def create(self, session_id):
        pass

    def destroy(self, session_id):
        self.destroyed.append(session_id)

    def materialize_github_repo(self, *args, **kwargs):
        pass

    def materialize_file(self, *args, **kwargs):
        pass


class FakeStore:
    def __init__(self):
        self.appended: list[tuple[str, str, dict | None]] = []

    def append(self, session_id, type, data=None):
        self.appended.append((session_id, type, data))
        raw = {"event_id": None, "type": type, "timestamp": "", **(data or {})}
        return event_from_persisted(raw, session_id)


class FakeBus:
    def __init__(self):
        self.published: list = []

    async def publish(self, event):
        self.published.append(event)

    def subscribe(self, event_filter):  # pragma: no cover - unused here
        raise NotImplementedError


def _make_session(session_id="sesn_del", status="idle"):
    s = Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_" + session_id,
    )
    s.status = status
    return s


def _make_uc(sessions, provisioner):
    store = FakeStore()
    bus = FakeBus()
    emitter = EventEmitter(store=store, bus=bus)
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        emitter=emitter,
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
    sessions = {"sesn_del": _make_session(status="idle")}
    provisioner = FakeProvisioner()
    uc, store, bus = _make_uc(sessions, provisioner)
    await uc.execute("sesn_del")
    assert store.appended == [("sesn_del", "session.deleted", {"final_status": "idle"})]
    assert len(bus.published) == 1
    assert bus.published[0].type == "session.deleted"
    assert bus.published[0].data == {"final_status": "idle"}


async def test_delete_session_not_found_raises():
    sessions: dict = {}
    provisioner = FakeProvisioner()
    uc, _, _ = _make_uc(sessions, provisioner)
    with pytest.raises(SessionNotFound):
        await uc.execute("sesn_missing")
