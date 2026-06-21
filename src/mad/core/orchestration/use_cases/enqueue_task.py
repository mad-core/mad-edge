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
from typing import Literal
from uuid import UUID, uuid4

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.ports.model_catalog import ModelCatalog
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class EnqueueTaskInput:
    session_id: str
    content: str
    scheduled_for: str = "now"
    model: str | None = None
    conversation_mode: Literal["new", "resume"] = "new"


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
        model_catalog: ModelCatalog | None = None,
    ) -> None:
        self._sessions = sessions_index
        self._emitter = emitter
        self._catalog = model_catalog

    async def execute(self, payload: EnqueueTaskInput) -> EnqueueTaskOutput:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)

        if payload.model is not None and self._catalog is not None:
            from mad.core.orchestration.use_cases.list_provider_models import (
                ListProviderModelsUseCase,
            )

            provider = self._sessions[payload.session_id].agent.get("provider", "")
            await ListProviderModelsUseCase(self._catalog).validate_model(provider, payload.model)

        task_id = uuid4()
        await self._emitter.emit(
            payload.session_id,
            "task.queued",
            {
                "task_id": str(task_id),
                "content": payload.content,
                "scheduled_for": payload.scheduled_for,
                "model": payload.model,
                "conversation_mode": payload.conversation_mode,
            },
        )
        return EnqueueTaskOutput(
            task_id=task_id,
            session_id=payload.session_id,
            scheduled_for=payload.scheduled_for,
        )
