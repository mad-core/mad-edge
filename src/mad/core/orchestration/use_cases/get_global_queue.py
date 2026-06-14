"""GetGlobalQueueUseCase — the cross-session queue view behind ``GET /v1/queue``.

Issue #46 Part C. The view is policy-aware and never flattens policy
groups into one priority-sorted list: a high-priority session whose
``work_window`` is closed (or whose policy is ``manual``) appears in
``scheduled`` with a reason, never in ``ready``. Three buckets:

- ``in_flight`` — the single dispatched task across all sessions
  (ADR-0009 Decision 4), or ``None``.
- ``ready`` — queued tasks from sessions dispatchable *right now*, in
  true dispatch order via the same ``order_ready_candidates`` the
  dispatcher uses (Part D): ``ready[0]`` is genuinely what runs next.
- ``scheduled`` — queued tasks from sessions not currently
  dispatchable, each with a reason (``window`` + the next window
  opening, or ``manual``), ordered by ``(scheduled_for, -priority)``.

Eligibility and the ``scheduled`` reasons are computed against the
*effective* policy — ``resolve_effective_policy(session, deployment)``
(issue #45) — exactly as the post-merge dispatcher evaluates it, so a
session inheriting a gated deployment default is bucketed and explained
the same way the dispatcher would treat it (ADR-0009 §11).

This read surface lives in ``core/orchestration/`` per ADR-0004 / hard
rule 8 — the events module stays observability-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from mad.core.orchestration.domain.deployment_policy import (
    DeploymentDispatchPolicy,
    resolve_effective_policy,
)
from mad.core.orchestration.domain.dispatch_policy import (
    WorkWindowPolicy,
    can_dispatch,
    next_window_opening,
)
from mad.core.orchestration.domain.ordering import order_ready_candidates
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.ports.clock import Clock
from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session

_DATETIME_FLOOR = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class QueueEntry:
    """A task annotated with its owning session's priority."""

    task: Task
    priority: int


@dataclass(frozen=True)
class ScheduledEntry:
    """A queued-but-gated task with the reason it is not in ``ready``."""

    task: Task
    priority: int
    reason_kind: Literal["window", "manual"]
    scheduled_for: datetime | None


@dataclass(frozen=True)
class GlobalQueueOutput:
    in_flight: QueueEntry | None
    ready: list[QueueEntry]
    scheduled: list[ScheduledEntry]


class GetGlobalQueueUseCase:
    """Assemble the global queue view from the projection and the live index."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        task_queue: TaskQueue,
        clock: Clock,
        deployment: DeploymentDispatchPolicy | None = None,
    ) -> None:
        self._sessions = sessions_index
        self._task_queue = task_queue
        self._clock = clock
        # Same holder the dispatcher resolves against (issue #45) — the
        # queue view must never disagree with the dispatcher (Part D).
        self._deployment = deployment

    def execute(self) -> GlobalQueueOutput:
        pending = self._task_queue.pending_session_ids()
        sessions: dict[str, Session] = {}
        for session_id in pending:
            session = self._sessions.get(session_id)
            if session is None:
                # Startup rehydration (Part A) plus enqueue's index check
                # guarantee every pending session is live. A miss means
                # the foundation is broken — fail loud (hard rule 7),
                # don't render a queue that silently omits work.
                raise RuntimeError(
                    f"session {session_id!r} has pending tasks but is not in the live session index"
                )
            sessions[session_id] = session

        in_flight = self._single_in_flight(pending, sessions)
        now = self._clock.now()
        eligible = [
            s
            for s in sessions.values()
            if can_dispatch(
                resolve_effective_policy(s, self._deployment),
                now,
                manual_drain_remaining=s.manual_drain_remaining,
            )
        ]
        eligible_ids = {s.session_id for s in eligible}
        gated = [s for s in sessions.values() if s.session_id not in eligible_ids]

        ready = [
            QueueEntry(task=task, priority=sessions[task.session_id].priority)
            for task in order_ready_candidates(eligible, self._task_queue)
        ]
        return GlobalQueueOutput(
            in_flight=in_flight,
            ready=ready,
            scheduled=self._scheduled(gated, now),
        )

    def _single_in_flight(
        self, pending: list[str], sessions: dict[str, Session]
    ) -> QueueEntry | None:
        dispatched = [
            task
            for session_id in pending
            if (task := self._task_queue.in_flight(session_id)) is not None
        ]
        if len(dispatched) > 1:
            ids = sorted(str(t.task_id) for t in dispatched)
            raise RuntimeError(
                f"single-dispatch invariant violated: {len(dispatched)} tasks "
                f"in flight ({', '.join(ids)})"
            )
        if not dispatched:
            return None
        task = dispatched[0]
        return QueueEntry(task=task, priority=sessions[task.session_id].priority)

    def _scheduled(self, gated: list[Session], now: datetime) -> list[ScheduledEntry]:
        entries: list[ScheduledEntry] = []
        for session in gated:
            policy = resolve_effective_policy(session, self._deployment)
            is_window = isinstance(policy, WorkWindowPolicy)
            scheduled_for = next_window_opening(policy, now) if is_window else None
            for task in self._task_queue.queued(session.session_id):
                entries.append(
                    ScheduledEntry(
                        task=task,
                        priority=session.priority,
                        reason_kind="window" if is_window else "manual",
                        scheduled_for=scheduled_for,
                    )
                )
        # (scheduled_for asc, nulls last, then -priority); created_at +
        # session_id keep within-session FIFO and make ties deterministic.
        entries.sort(
            key=lambda e: (
                e.scheduled_for is None,
                e.scheduled_for or _DATETIME_FLOOR,
                -e.priority,
                e.task.created_at,
                e.task.session_id,
            )
        )
        return entries
