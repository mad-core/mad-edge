"""DeleteSession use case and the underlying destroy primitive.

The module exports two surfaces:

- ``destroy_session(session, provisioner, emitter)`` — async primitive that
  destroys the workspace, marks the entity ``deleted``, and emits
  ``session.deleted``. Stateless w.r.t. the in-memory index, so both the
  single-session use case and the bulk cleanup use case call it without
  redoing the index lookup.
- ``DeleteSessionUseCase`` — public single-session entry point. Looks up
  the entity in the index (raising ``SessionNotFound`` if absent) and then
  delegates to ``destroy_session``.
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.events.emitter import EventEmitter
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner


@dataclass
class DeleteSessionOutput:
    session_id: str
    status: str


async def destroy_session(
    session: Session,
    provisioner: WorkspaceProvisioner,
    emitter: EventEmitter,
) -> str:
    """Destroy ``session``'s workspace, mark it deleted, emit ``session.deleted``.

    Returns the prior status (the value before ``mark_deleted`` runs), which
    callers surface as ``final_status`` in the emitted event payload.
    """
    prior_status = session.status
    provisioner.destroy(session.session_id)
    session.mark_deleted()
    await emitter.emit(session.session_id, "session.deleted", {"final_status": prior_status})
    return prior_status


class DeleteSessionUseCase:
    """Delete a single session and destroy its workspace."""

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
        await destroy_session(session, self._provisioner, self._emitter)
        return DeleteSessionOutput(session_id=session_id, status="deleted")
