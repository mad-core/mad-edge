"""TriggerManualDispatchUseCase — drain the queue once, manual mode only.

Per ADR-0009 §9: ``POST /v1/sessions/{id}/dispatch_policy/trigger``
captures the current queue length and sets
``Session.manual_drain_remaining`` to that count. The dispatcher's
``can_dispatch`` returns True for the next N dispatches; after they
fire the counter returns to 0 and the policy is once again "queue
accumulates, dispatcher does nothing autonomously."

In ``immediate`` / ``work_window`` modes the trigger is meaningless —
the dispatcher already dispatches automatically. We return 409 so the
caller knows the policy is wrong, not silently no-op.
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.orchestration.domain.dispatch_policy import ManualPolicy
from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


class TriggerNotApplicable(Exception):
    """Raised when a manual trigger arrives for a non-manual policy.

    The HTTP route maps this to 409 with a detail naming the active
    policy kind.
    """

    def __init__(self, session_id: str, kind: str) -> None:
        super().__init__(
            f"manual trigger does not apply to session {session_id} with dispatch policy {kind!r}"
        )
        self.session_id = session_id
        self.kind = kind


@dataclass(frozen=True)
class TriggerManualDispatchInput:
    session_id: str


@dataclass(frozen=True)
class TriggerManualDispatchOutput:
    session_id: str
    drained: int


class TriggerManualDispatchUseCase:
    """Trigger one drain pass on a manual-mode session."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        task_queue: TaskQueue,
    ) -> None:
        self._sessions = sessions_index
        self._queue = task_queue

    def execute(self, payload: TriggerManualDispatchInput) -> TriggerManualDispatchOutput:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)
        session = self._sessions[payload.session_id]

        if not isinstance(session.dispatch_policy, ManualPolicy):
            raise TriggerNotApplicable(payload.session_id, session.dispatch_policy.kind)

        # Snapshot the queued count at trigger time. New tasks queued after
        # the trigger but before drain finishes do NOT join this drain.
        queued_count = len(self._queue.queued(payload.session_id))
        session.manual_drain_remaining = queued_count

        return TriggerManualDispatchOutput(
            session_id=payload.session_id,
            drained=queued_count,
        )
