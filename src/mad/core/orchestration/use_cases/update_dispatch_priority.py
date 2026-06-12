"""UpdateDispatchPriorityUseCase — set a session's cross-session priority.

Issue #46 Part B, mirroring ``update_dispatch_policy``: every successful
PATCH emits ``dispatch_priority.updated`` so a process restart rebuilds
``Session.priority`` by replaying the event log — the JSONL log is the
only durable record (hard rule 6). Priority is set ONLY through this
use case; task content is never inspected to derive it (hard rule 1).
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.ordering import validate_priority
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound


@dataclass(frozen=True)
class UpdateDispatchPriorityInput:
    session_id: str
    priority: int


@dataclass(frozen=True)
class UpdateDispatchPriorityOutput:
    session_id: str
    priority: int


class UpdateDispatchPriorityUseCase:
    """Set a session's dispatch priority and persist via event log."""

    def __init__(
        self,
        sessions_index: dict[str, Session],
        emitter: EventEmitter,
    ) -> None:
        self._sessions = sessions_index
        self._emitter = emitter

    async def execute(self, payload: UpdateDispatchPriorityInput) -> UpdateDispatchPriorityOutput:
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)
        # The HTTP boundary already rejects out-of-range values via
        # Pydantic; this guard keeps non-HTTP callers (future MCP tools,
        # scripts) from writing an unreplayable event.
        priority = validate_priority(payload.priority)

        session = self._sessions[payload.session_id]
        session.priority = priority

        await self._emitter.emit(
            payload.session_id,
            "dispatch_priority.updated",
            {"priority": priority},
        )

        return UpdateDispatchPriorityOutput(
            session_id=payload.session_id,
            priority=priority,
        )
