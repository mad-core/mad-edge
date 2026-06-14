"""DeleteSession use case and the underlying destroy primitive.

The module exports two surfaces:

- ``destroy_session(session, provisioner, emitter, task_queue)`` — async
  primitive that cancels the session's still-queued tasks, destroys the
  workspace, marks the entity ``deleted``, and emits ``session.deleted``.
  Stateless w.r.t. the in-memory index, so both the single-session use
  case and the bulk cleanup use case call it without redoing the index
  lookup.
- ``DeleteSessionUseCase`` — public single-session entry point. Looks up
  the entity in the index (raising ``SessionNotFound`` if absent) and then
  delegates to ``destroy_session``.

Queued tasks are cancelled (not silently dropped) because deletion only
``mark_deleted()``s the entity — it stays in the live index — and the
cross-session queue (``GET /v1/queue``) and the dispatcher are scoped by
``TaskQueue.pending_session_ids()``, not by session status. Without an
explicit ``task.cancelled``, a deleted session's queued task lingers in
the ``scheduled`` bucket and, once its work-window opens, the dispatcher
would launch it against a workspace that ``provisioner.destroy`` already
removed (issue #46). Emitting ``task.cancelled`` keeps the log
authoritative (hard rule 6) so the orphan stays gone across restarts.
"""

from __future__ import annotations

from dataclasses import dataclass

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.ports.task_queue import TaskQueue
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
    task_queue: TaskQueue,
) -> str:
    """Cancel queued tasks, destroy the workspace, mark deleted, emit ``session.deleted``.

    Returns the prior status (the value before ``mark_deleted`` runs), which
    callers surface as ``final_status`` in the emitted event payload.
    """
    prior_status = session.status
    # Cancel queued tasks before tearing down so they never outlive the
    # session in the cross-session queue / dispatcher (issue #46). In-flight
    # tasks are left alone: the running launcher resolves them to
    # task.completed/failed on its own.
    for task in task_queue.queued(session.session_id):
        await emitter.emit(
            session.session_id,
            "task.cancelled",
            {"task_id": str(task.task_id), "reason": "session_deleted"},
        )
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
        task_queue: TaskQueue,
    ) -> None:
        self._provisioner = provisioner
        self._sessions = sessions_index
        self._emitter = emitter
        self._task_queue = task_queue

    async def execute(self, session_id: str) -> DeleteSessionOutput:
        if session_id not in self._sessions:
            raise SessionNotFound(session_id)

        session = self._sessions[session_id]
        await destroy_session(session, self._provisioner, self._emitter, self._task_queue)
        return DeleteSessionOutput(session_id=session_id, status="deleted")
