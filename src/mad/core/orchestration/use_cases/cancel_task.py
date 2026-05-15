"""CancelTaskUseCase — emit ``task.cancelled`` for a queued task.

The use case rejects two error modes the HTTP route maps to status
codes (ADR-0009 Decision 6):

- Task is in flight (currently dispatched) → ``TaskAlreadyDispatched``
  → 409. v1 does not cancel running launches.
- Task does not exist on this session → ``TaskNotFound`` → 404.

A queued task is removed from the queue by emitting
``task.cancelled``; the projection (Phase 4) interprets the event.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.exceptions.base import (
    TaskAlreadyDispatched,
    TaskNotFound,
)
from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class CancelTaskInput:
    session_id: str
    task_id: UUID
    reason: str = "user_cancelled"


class CancelTaskUseCase:
    """Cancel a queued task by emitting ``task.cancelled``."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        task_queue: TaskQueue,
        emitter: EventEmitter,
    ) -> None:
        self._sessions = sessions_index
        self._queue = task_queue
        self._emitter = emitter

    async def execute(self, payload: CancelTaskInput) -> None:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)

        in_flight = self._queue.in_flight(payload.session_id)
        if in_flight is not None and in_flight.task_id == payload.task_id:
            raise TaskAlreadyDispatched(payload.task_id)

        queued = self._queue.queued(payload.session_id)
        if not any(t.task_id == payload.task_id for t in queued):
            raise TaskNotFound(payload.task_id)

        await self._emitter.emit(
            payload.session_id,
            "task.cancelled",
            {"task_id": str(payload.task_id), "reason": payload.reason},
        )
