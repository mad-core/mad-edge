"""Unit tests for ``order_ready_candidates`` / ``validate_priority`` (issue #46).

This is the single ordering function shared by the dispatcher and the
``GET /v1/queue`` ``ready`` builder (Part D) — the contract pinned here
is exactly what dispatches: priority descending, head-task ``arrived_at``
(``Task.created_at``) ascending on ties, within-session FIFO untouched,
and task content NEVER consulted (hard rule 1).

Heuristic 1 — every ordering rule has its negative twin (the losing
side asserted explicitly, not just the winner).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from mad.core.orchestration.domain.ordering import (
    DEFAULT_PRIORITY,
    InvalidPriority,
    order_ready_candidates,
    validate_priority,
)
from mad.core.orchestration.domain.task import Task
from mad.core.sessions.domain.entities.session import Session
from support.orchestration import FakeTaskQueue


def _session(session_id: str, priority: int = DEFAULT_PRIORITY) -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_t",
        tokens_to_redact=[],
        priority=priority,
    )


def _task(session_id: str, *, minute: int, content: str = "opaque") -> Task:
    return Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
    )


# -- Priority ordering ----------------------------------------------------------


def test_higher_priority_session_head_dispatches_first() -> None:
    """Priority beats arrival: the high-priority head wins even though it
    arrived AFTER the low-priority one."""
    low = _session("sesn_low", priority=1)
    high = _session("sesn_high", priority=5)
    low_task = _task("sesn_low", minute=0)
    high_task = _task("sesn_high", minute=30)
    queue = FakeTaskQueue(queued={"sesn_low": [low_task], "sesn_high": [high_task]})

    ordered = order_ready_candidates([low, high], queue)

    assert [t.task_id for t in ordered] == [high_task.task_id, low_task.task_id]


def test_lower_priority_session_does_not_dispatch_first() -> None:
    """Negative twin: the lower-priority session's head is NOT first, even
    though its session was passed first AND its task arrived earlier —
    neither argument order nor arrival can outrank priority."""
    low = _session("sesn_low", priority=2)
    high = _session("sesn_high", priority=9)
    low_task = _task("sesn_low", minute=0)
    high_task = _task("sesn_high", minute=59)
    queue = FakeTaskQueue(queued={"sesn_low": [low_task], "sesn_high": [high_task]})

    ordered = order_ready_candidates([low, high], queue)

    assert ordered[0].task_id != low_task.task_id
    assert ordered[0].task_id == high_task.task_id


def test_equal_priority_ties_break_on_earlier_head_arrived_at() -> None:
    a = _session("sesn_a", priority=3)
    b = _session("sesn_b", priority=3)
    early = _task("sesn_b", minute=5)
    late = _task("sesn_a", minute=40)
    queue = FakeTaskQueue(queued={"sesn_a": [late], "sesn_b": [early]})

    ordered = order_ready_candidates([a, b], queue)

    assert [t.task_id for t in ordered] == [early.task_id, late.task_id]


def test_equal_priority_later_arrival_does_not_win() -> None:
    """Negative twin: passing the later-arrival session first must not
    promote it — ties are arrival-ordered, not insertion-ordered."""
    a = _session("sesn_a", priority=3)
    b = _session("sesn_b", priority=3)
    late = _task("sesn_a", minute=40)
    early = _task("sesn_b", minute=5)
    queue = FakeTaskQueue(queued={"sesn_a": [late], "sesn_b": [early]})

    ordered = order_ready_candidates([a, b], queue)

    assert ordered[0].task_id != late.task_id


def test_within_session_order_stays_fifo_even_against_timestamps() -> None:
    """The queue's insertion order IS the within-session contract; a
    clock-skewed earlier ``created_at`` on a later task must not reorder
    it ahead of its predecessor."""
    a = _session("sesn_a", priority=3)
    first_queued = _task("sesn_a", minute=30)
    second_queued = _task("sesn_a", minute=10)  # earlier timestamp, queued later
    queue = FakeTaskQueue(queued={"sesn_a": [first_queued, second_queued]})

    ordered = order_ready_candidates([a], queue)

    assert [t.task_id for t in ordered] == [first_queued.task_id, second_queued.task_id]


def test_equal_priority_sessions_interleave_in_simulated_dispatch_order() -> None:
    """After a session's head dispatches, it re-competes with its NEXT
    task's arrival time: a1(12:00), b1(12:30), a2(12:50) — not a1, a2, b1."""
    a = _session("sesn_a", priority=3)
    b = _session("sesn_b", priority=3)
    a1 = _task("sesn_a", minute=0)
    a2 = _task("sesn_a", minute=50)
    b1 = _task("sesn_b", minute=30)
    queue = FakeTaskQueue(queued={"sesn_a": [a1, a2], "sesn_b": [b1]})

    ordered = order_ready_candidates([a, b], queue)

    assert [t.task_id for t in ordered] == [a1.task_id, b1.task_id, a2.task_id]


def test_task_content_is_never_used_for_ordering() -> None:
    """Hard rule 1 negative twin: content is crafted so that ANY
    content-based ranking (urgency keywords, lexicographic) would invert
    the result — the priority/arrival contract must still hold."""
    low = _session("sesn_low", priority=1)
    high = _session("sesn_high", priority=8)
    screaming = _task("sesn_low", minute=0, content="URGENT P0 CRITICAL drop everything")
    mundane = _task("sesn_high", minute=45, content="zzz trivial chore, no rush")
    queue = FakeTaskQueue(queued={"sesn_low": [screaming], "sesn_high": [mundane]})

    ordered = order_ready_candidates([low, high], queue)

    assert [t.task_id for t in ordered] == [mundane.task_id, screaming.task_id]


def test_sessions_without_queued_tasks_yield_nothing() -> None:
    busy = _session("sesn_busy", priority=5)
    idle = _session("sesn_idle", priority=9)
    task = _task("sesn_busy", minute=0)
    queue = FakeTaskQueue(queued={"sesn_busy": [task]})

    ordered = order_ready_candidates([busy, idle], queue)

    assert [t.task_id for t in ordered] == [task.task_id]


def test_no_eligible_sessions_returns_empty_list() -> None:
    queue = FakeTaskQueue()
    assert order_ready_candidates([], queue) == []


# -- validate_priority -----------------------------------------------------------


@pytest.mark.parametrize("value", [1, 5, 10])
def test_validate_priority_accepts_in_range_values(value: int) -> None:
    assert validate_priority(value) == value


@pytest.mark.parametrize("value", [0, 11, -5])
def test_validate_priority_rejects_out_of_range(value: int) -> None:
    """Negative twin: out-of-range is REJECTED, never silently clamped."""
    with pytest.raises(InvalidPriority, match=r"\[1, 10\]"):
        validate_priority(value)


@pytest.mark.parametrize("value", ["5", 5.0, None, True])
def test_validate_priority_rejects_non_int_values(value: object) -> None:
    with pytest.raises(InvalidPriority, match="int"):
        validate_priority(value)  # type: ignore[arg-type]
