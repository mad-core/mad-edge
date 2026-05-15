"""ListTasksUseCase — read-side wrapper over the ``TaskQueue`` port.

Returns the per-session ``{queued, in_flight}`` view so the HTTP route
can serialise it. The work happens entirely in the projection
(ADR-0009 Decision 3); this use case exists to centralise the session-
existence check and keep the route thin.
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class ListTasksOutput:
    queued: list[Task]
    in_flight: Task | None


class ListTasksUseCase:
    """Return the queue + in-flight task for a known session."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        task_queue: TaskQueue,
    ) -> None:
        self._sessions = sessions_index
        self._queue = task_queue

    def execute(self, session_id: str) -> ListTasksOutput:
        if session_id not in self._sessions:
            raise SessionNotFound(session_id)
        return ListTasksOutput(
            queued=self._queue.queued(session_id),
            in_flight=self._queue.in_flight(session_id),
        )
