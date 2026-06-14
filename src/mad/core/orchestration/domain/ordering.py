"""Cross-session dispatch ordering (issue #46 / ADR-0009 §10).

One pure function decides the order in which queued tasks reach the
launcher across sessions. Both consumers MUST go through it so the
operator-facing queue view never disagrees with what the dispatcher
actually picks:

- ``Dispatcher._find_next_dispatchable`` takes ``[0]``.
- ``GetGlobalQueueUseCase`` returns the whole list as the ``ready``
  bucket of ``GET /v1/queue``.

Ordering model: sessions are ranked by ``(-priority, head_task
.created_at, session_id)`` — priority descending, then the head queued
task's arrival time ascending (the ``task.queued`` event timestamp),
then session id as a deterministic final tiebreak. Within a session,
order stays FIFO (hard-rule: the ``Task`` entity and per-session
semantics are unchanged). Task ``content`` is never inspected
(ADR-0009 Decision 7 / hard rule 1).

Priority bounds live here so the Session entity, the HTTP boundary,
and the replay path share one definition.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mad.core.orchestration.domain.task import Task

if TYPE_CHECKING:
    # Typing-only: Session imports DEFAULT_PRIORITY from this module, so a
    # runtime import here would be circular.
    from mad.core.orchestration.ports.task_queue import TaskQueue
    from mad.core.sessions.domain.entities.session import Session

MIN_PRIORITY = 1
MAX_PRIORITY = 10
DEFAULT_PRIORITY = 1


class InvalidPriority(ValueError):
    """Raised for a priority outside ``[MIN_PRIORITY, MAX_PRIORITY]``.

    Inherits from ``ValueError``; the HTTP app maps it to 422 so an
    out-of-range value is rejected loudly, never clamped silently.
    """


def validate_priority(value: object) -> int:
    """Return ``value`` if it is an int within bounds; raise otherwise."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidPriority(f"priority must be an int, got {value!r}")
    if not (MIN_PRIORITY <= value <= MAX_PRIORITY):
        raise InvalidPriority(f"priority must be in [{MIN_PRIORITY}, {MAX_PRIORITY}], got {value}")
    return value


def order_ready_candidates(
    eligible_sessions: list[Session],
    projection: TaskQueue,
) -> list[Task]:
    """Return every queued task of ``eligible_sessions`` in true dispatch order.

    Simulates the dispatcher's repeated pick: at each step the session
    with the best ``(-priority, head.created_at, session_id)`` key
    yields its head task. ``result[0]`` is what dispatches next;
    ``result[n]`` is the n-th dispatch assuming no new arrivals. The
    simulation matters for sessions with several queued tasks — after a
    head is taken, the session re-competes with its *next* task's
    arrival time, which can interleave it behind an equal-priority
    session that arrived later than the old head but earlier than the
    new one.
    """
    queues: list[tuple[Session, list[Task]]] = []
    for session in eligible_sessions:
        queued = projection.queued(session.session_id)
        if queued:
            queues.append((session, queued))

    ordered: list[Task] = []
    cursors = {session.session_id: 0 for session, _ in queues}
    remaining = dict.fromkeys(cursors)  # insertion-ordered session_id set
    by_id = {session.session_id: (session, tasks) for session, tasks in queues}
    while remaining:
        best_id = min(
            remaining,
            key=lambda sid: (
                -by_id[sid][0].priority,
                by_id[sid][1][cursors[sid]].created_at,
                sid,
            ),
        )
        session, tasks = by_id[best_id]
        ordered.append(tasks[cursors[best_id]])
        cursors[best_id] += 1
        if cursors[best_id] >= len(tasks):
            del remaining[best_id]
    return ordered
