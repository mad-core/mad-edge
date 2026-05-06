"""DeleteSession use case."""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.events.emitter import EventEmitter
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner


@dataclass
class DeleteSessionOutput:
    session_id: str
    status: str


class DeleteSessionUseCase:
    """Delete a session and destroy its workspace."""

    def __init__(
        self,
        provisioner: WorkspaceProvisioner,
        sessions_index: dict[str, Session],
        emitter: EventEmitter,
    ) -> None:
        self._provisioner = provisioner
        self._sessions = sessions_index
        self._emitter = emitter

    async def execute(self, session_id: str) -> DeleteSessionOutput:
        if session_id not in self._sessions:
            raise SessionNotFound(session_id)

        session = self._sessions[session_id]
        prior_status = session.status
        self._provisioner.destroy(session_id)
        session.mark_deleted()
        await self._emitter.emit(
            session_id, "session.deleted", {"final_status": prior_status}
        )

        return DeleteSessionOutput(session_id=session_id, status="deleted")
