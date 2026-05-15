"""Integration tests for ``InMemoryTaskProjection``.

The projection is the load-bearing read-side abstraction: every
``GET /v1/sessions/{id}/tasks`` and every dispatcher decision flows
through ``queued()`` / ``in_flight()``. The tests exercise both entry
points (``bootstrap_from_log`` against a ``FakeEventLogQuery``, and
``apply()`` for the live-tail case), the full task lifecycle, and the
edge cases ADR-0009 Decision 5 / 6 documents.

Heuristic 1 — every happy path has a negative twin (e.g. dispatched
without a matching queued task; cancel before dispatch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.domain.event import Event
from support.events import FakeEventLogQuery


def _seq_event_id(seq: int) -> UUID:
    """Deterministic UUIDv7-shaped event_id ordered by ``seq``.

    The 48-bit ``seq`` populates the timestamp prefix so lexicographic
    string compare matches insertion order — ``FakeEventLogQuery`` sorts
    on that string, so fully-random ``uuid4`` ids produce non-deterministic
    bootstrap ordering. See ADR-0005 for the production semantics this
    mimics."""
    return UUID(int=(seq << 80) | (0x7 << 76) | (0b10 << 62))


def _event(
    *,
    type: str,
    session_id: str,
    task_id: UUID | None = None,
    content: str = "opaque",
    scheduled_for: str = "now",
    reason: str | None = None,
    timestamp: datetime | None = None,
    event_id: UUID | None = None,
    seq: int | None = None,
) -> Event:
    data: dict = {}
    if task_id is not None:
        data["task_id"] = str(task_id)
    if type == "task.queued":
        data["content"] = content
        data["scheduled_for"] = scheduled_for
    if reason is not None:
        data["reason"] = reason
    if seq is not None:
        chosen_event_id = _seq_event_id(seq)
    elif event_id is not None:
        chosen_event_id = event_id
    else:
        chosen_event_id = uuid4()
    return Event(
        event_id=chosen_event_id,
        session_id=session_id,
        type=type,
        data=data,
        timestamp=timestamp if timestamp is not None else datetime(2026, 5, 8, tzinfo=UTC),
    )


# -- apply() — live-tail path ------------------------------------------------


def test_task_queued_appears_in_queued_list_in_insertion_order() -> None:
    proj = InMemoryTaskProjection()
    a, b = uuid4(), uuid4()

    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a, content="first"))
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=b, content="second"))

    queued = proj.queued("sesn_a")
    assert [t.task_id for t in queued] == [a, b]
    assert [t.content for t in queued] == ["first", "second"]
    assert proj.in_flight("sesn_a") is None


def test_task_dispatched_moves_task_from_queued_to_in_flight() -> None:
    proj = InMemoryTaskProjection()
    a, b = uuid4(), uuid4()
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a, content="A"))
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=b, content="B"))

    proj.apply(_event(type="task.dispatched", session_id="sesn_a", task_id=a))

    in_flight = proj.in_flight("sesn_a")
    assert in_flight is not None
    assert in_flight.task_id == a
    assert [t.task_id for t in proj.queued("sesn_a")] == [b]


def test_task_completed_clears_in_flight_slot() -> None:
    proj = InMemoryTaskProjection()
    a = uuid4()
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a))
    proj.apply(_event(type="task.dispatched", session_id="sesn_a", task_id=a))

    proj.apply(_event(type="task.completed", session_id="sesn_a", task_id=a))

    assert proj.in_flight("sesn_a") is None
    assert proj.queued("sesn_a") == []


def test_task_failed_clears_in_flight_slot() -> None:
    proj = InMemoryTaskProjection()
    a = uuid4()
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a))
    proj.apply(_event(type="task.dispatched", session_id="sesn_a", task_id=a))

    proj.apply(
        _event(
            type="task.failed",
            session_id="sesn_a",
            task_id=a,
            reason="interrupted_by_restart",
        )
    )

    assert proj.in_flight("sesn_a") is None


def test_task_cancelled_removes_queued_task_without_dispatching() -> None:
    proj = InMemoryTaskProjection()
    a, b = uuid4(), uuid4()
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a, content="A"))
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=b, content="B"))

    proj.apply(
        _event(
            type="task.cancelled",
            session_id="sesn_a",
            task_id=a,
            reason="user_cancelled",
        )
    )

    assert [t.task_id for t in proj.queued("sesn_a")] == [b]
    assert proj.in_flight("sesn_a") is None


def test_dispatched_without_matching_queued_is_a_safe_noop() -> None:
    """Edge case: a stray task.dispatched with no prior task.queued
    leaves the projection unchanged. The dispatcher (Phase 5) is
    expected to maintain the queued→dispatched ordering; this test pins
    the projection's tolerance so a future dispatcher bug doesn't
    corrupt state."""
    proj = InMemoryTaskProjection()
    stray = uuid4()

    proj.apply(_event(type="task.dispatched", session_id="sesn_a", task_id=stray))

    assert proj.queued("sesn_a") == []
    assert proj.in_flight("sesn_a") is None


def test_terminal_event_for_unknown_task_is_a_safe_noop() -> None:
    proj = InMemoryTaskProjection()
    unknown = uuid4()

    proj.apply(_event(type="task.cancelled", session_id="sesn_a", task_id=unknown, reason="x"))

    assert proj.queued("sesn_a") == []
    assert proj.in_flight("sesn_a") is None


def test_non_task_event_is_ignored() -> None:
    proj = InMemoryTaskProjection()

    proj.apply(_event(type="agent.output", session_id="sesn_a"))
    proj.apply(_event(type="session.status_idle", session_id="sesn_a"))

    assert proj.queued("sesn_a") == []
    assert proj.in_flight("sesn_a") is None


def test_state_is_isolated_per_session() -> None:
    proj = InMemoryTaskProjection()
    a, b = uuid4(), uuid4()
    proj.apply(_event(type="task.queued", session_id="sesn_a", task_id=a, content="A"))
    proj.apply(_event(type="task.queued", session_id="sesn_b", task_id=b, content="B"))

    assert [t.task_id for t in proj.queued("sesn_a")] == [a]
    assert [t.task_id for t in proj.queued("sesn_b")] == [b]
    assert proj.in_flight("sesn_a") is None
    assert proj.in_flight("sesn_b") is None


# -- bootstrap_from_log() — startup recovery path ----------------------------


def test_bootstrap_replays_full_lifecycle_from_log() -> None:
    """The bootstrap replays events in mint order. ``seq`` here drives a
    deterministic UUIDv7-shaped event_id so ``FakeEventLogQuery``'s
    lex-sort matches insertion order; with random uuid4 the test would
    flake (~50% on this 5-event sequence)."""
    a, b, c = uuid4(), uuid4(), uuid4()
    events = [
        _event(type="task.queued", session_id="sesn_a", task_id=a, content="A", seq=1),
        _event(type="task.queued", session_id="sesn_a", task_id=b, content="B", seq=2),
        _event(type="task.dispatched", session_id="sesn_a", task_id=a, seq=3),
        _event(type="task.completed", session_id="sesn_a", task_id=a, seq=4),
        _event(type="task.queued", session_id="sesn_a", task_id=c, content="C", seq=5),
    ]
    log = FakeEventLogQuery(events=events)
    proj = InMemoryTaskProjection()

    proj.bootstrap_from_log(log)

    queued = proj.queued("sesn_a")
    assert [t.task_id for t in queued] == [b, c]
    assert [t.content for t in queued] == ["B", "C"]
    assert proj.in_flight("sesn_a") is None


def test_bootstrap_preserves_in_flight_when_terminal_event_missing() -> None:
    """Crash-recovery scenario per ADR-0009 Decision 5: a task.dispatched
    without a matching terminal event is the orphan signal. The
    projection's job is just to materialise that state — the orphan
    cleanup (emitting `task.failed` with `interrupted_by_restart`)
    happens elsewhere in the dispatcher."""
    a = uuid4()
    events = [
        _event(type="task.queued", session_id="sesn_a", task_id=a, content="A", seq=1),
        _event(type="task.dispatched", session_id="sesn_a", task_id=a, seq=2),
    ]
    log = FakeEventLogQuery(events=events)
    proj = InMemoryTaskProjection()

    proj.bootstrap_from_log(log)

    in_flight = proj.in_flight("sesn_a")
    assert in_flight is not None
    assert in_flight.task_id == a
    assert proj.queued("sesn_a") == []


def test_bootstrap_on_empty_log_yields_empty_projection() -> None:
    proj = InMemoryTaskProjection()
    proj.bootstrap_from_log(FakeEventLogQuery(events=[]))

    assert proj.queued("sesn_a") == []
    assert proj.in_flight("sesn_a") is None
