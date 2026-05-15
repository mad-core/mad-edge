"""EnqueueTaskUseCase — accept a task and emit ``task.queued``.

Per ADR-0009 the task is opaque (Decision 7 / hard rule 1): ``content``
is recorded verbatim and never inspected. Per hard rule 11 every event
goes through ``EventEmitter.emit()`` — the use case never calls the
underlying ``EventStore`` or ``EventBus``.

The dispatcher (Phase 5) reacts to ``task.queued`` via the projection;
this use case is purely the *intake* of new tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from mad.core.events.emitter import EventEmitter
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class EnqueueTaskInput:
    session_id: str
    content: str
    scheduled_for: str = "now"


@dataclass(frozen=True)
class EnqueueTaskOutput:
    task_id: UUID
    session_id: str
    scheduled_for: str


class EnqueueTaskUseCase:
    """Append a ``task.queued`` event for a known session."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        emitter: EventEmitter,
    ) -> None:
        self._sessions = sessions_index
        self._emitter = emitter

    async def execute(self, payload: EnqueueTaskInput) -> EnqueueTaskOutput:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)

        task_id = uuid4()
        await self._emitter.emit(
            payload.session_id,
            "task.queued",
            {
                "task_id": str(task_id),
                "content": payload.content,
                "scheduled_for": payload.scheduled_for,
            },
        )
        return EnqueueTaskOutput(
            task_id=task_id,
            session_id=payload.session_id,
            scheduled_for=payload.scheduled_for,
        )
