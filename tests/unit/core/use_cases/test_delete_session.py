"""Unit tests for DeleteSessionUseCase."""

from __future__ import annotations

import pytest

from mad.core.domain.entities.session import Session
from mad.core.domain.exceptions.base import SessionNotFound
from mad.core.use_cases.sessions.delete_session import DeleteSessionUseCase


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


def _make_session(session_id="sesn_del"):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_" + session_id,
    )


def test_delete_session_happy_path():
    sessions = {"sesn_del": _make_session()}
    provisioner = FakeProvisioner()
    sse_queues = {}
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        sse_queues=sse_queues,
    )
    out = uc.execute("sesn_del")
    assert out.status == "deleted"
    assert out.session_id == "sesn_del"
    assert sessions["sesn_del"].status == "deleted"
    assert "sesn_del" in provisioner.destroyed


def test_delete_session_cleans_sse_queue():
    import asyncio

    sessions = {"sesn_del": _make_session()}
    provisioner = FakeProvisioner()
    q = asyncio.Queue()
    sse_queues = {"sesn_del": q}
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        sse_queues=sse_queues,
    )
    uc.execute("sesn_del")
    assert "sesn_del" not in sse_queues


def test_delete_session_not_found_raises():
    sessions: dict = {}
    provisioner = FakeProvisioner()
    uc = DeleteSessionUseCase(
        provisioner=provisioner,
        sessions_index=sessions,
        sse_queues={},
    )
    with pytest.raises(SessionNotFound):
        uc.execute("sesn_missing")
