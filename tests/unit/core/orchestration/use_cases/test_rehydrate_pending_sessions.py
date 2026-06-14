"""Unit tests for ``rehydrate_pending_sessions`` (issue #46 Part A).

Contract: every session the projection reports as having pending work
(queued or in-flight) is rebuilt from its JSONL events into the live
index BEFORE the dispatcher starts; sessions without pending work stay
lazy-loaded; live sessions are never overwritten by a replay; a pending
session without a persisted log is an invariant violation that fails
loud, not a silent skip.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.rehydrate_pending_sessions import (
    rehydrate_pending_sessions,
)
from mad.core.sessions.domain.entities.session import Session
from support.orchestration import FakeTaskQueue
from support.sessions import FakeSessionRepository


def _task(session_id: str) -> Task:
    return Task(
        task_id=uuid4(),
        session_id=session_id,
        content="opaque",
        scheduled_for="now",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )


def _seed_log(repo: FakeSessionRepository, session_id: str, *, priority: int | None = None) -> None:
    repo.append_event(
        session_id,
        "session.created",
        {
            "agent": "t",
            "working_directory": f"/tmp/mad_{session_id}",
            "timestamp": "2026-06-01T10:00:00+00:00",
        },
    )
    if priority is not None:
        repo.append_event(session_id, "dispatch_priority.updated", {"priority": priority})


def test_sessions_with_queued_work_are_inserted_into_the_index() -> None:
    repo = FakeSessionRepository()
    _seed_log(repo, "sesn_a", priority=7)
    queue = FakeTaskQueue(queued={"sesn_a": [_task("sesn_a")]})
    index: dict[str, Session] = {}

    rehydrated = rehydrate_pending_sessions(queue, repo, index)

    assert rehydrated == ["sesn_a"]
    assert index["sesn_a"].session_id == "sesn_a"
    # The replay carries the persisted priority — the dispatcher orders
    # by it immediately at boot, before any per-session fetch.
    assert index["sesn_a"].priority == 7


def test_sessions_with_in_flight_work_are_inserted_into_the_index() -> None:
    """In-flight counts as pending: orphan recovery walks the index, so a
    crashed-mid-run session MUST be rehydrated for its orphan to be found."""
    repo = FakeSessionRepository()
    _seed_log(repo, "sesn_b")
    queue = FakeTaskQueue(in_flight={"sesn_b": _task("sesn_b")})
    index: dict[str, Session] = {}

    rehydrated = rehydrate_pending_sessions(queue, repo, index)

    assert rehydrated == ["sesn_b"]
    assert index["sesn_b"].status == "created"


def test_session_without_pending_work_is_not_rehydrated() -> None:
    """Negative twin: idle history stays lazy-loaded — only pending-work
    sessions enter the index at boot."""
    repo = FakeSessionRepository()
    _seed_log(repo, "sesn_idle")
    _seed_log(repo, "sesn_busy")
    queue = FakeTaskQueue(queued={"sesn_busy": [_task("sesn_busy")]})
    index: dict[str, Session] = {}

    rehydrated = rehydrate_pending_sessions(queue, repo, index)

    assert rehydrated == ["sesn_busy"]
    assert "sesn_idle" not in index


def test_live_session_in_the_index_is_not_overwritten_by_replay() -> None:
    repo = FakeSessionRepository()
    _seed_log(repo, "sesn_live")
    queue = FakeTaskQueue(queued={"sesn_live": [_task("sesn_live")]})
    live = Session(
        session_id="sesn_live",
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_live",
        tokens_to_redact=[],
        priority=9,
    )
    index = {"sesn_live": live}

    rehydrated = rehydrate_pending_sessions(queue, repo, index)

    assert rehydrated == []
    assert index["sesn_live"] is live


def test_pending_session_without_persisted_log_fails_loud() -> None:
    """Negative twin for hard rule 7: the projection is built from the same
    log the repo reads — a pending session with no log means the foundation
    is broken, and that must surface, not be papered over."""
    repo = FakeSessionRepository()
    queue = FakeTaskQueue(queued={"sesn_ghost": [_task("sesn_ghost")]})

    with pytest.raises(RuntimeError, match="sesn_ghost"):
        rehydrate_pending_sessions(queue, repo, {})
