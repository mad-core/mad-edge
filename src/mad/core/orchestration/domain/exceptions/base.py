"""Domain exceptions for the orchestration module.

Use cases raise these; the inbound HTTP layer maps them to status
codes (404 / 409). Mirrors the pattern at
``mad.core.sessions.domain.exceptions``.
"""

from __future__ import annotations

from uuid import UUID


class TaskNotFound(Exception):
    """Raised when a ``task_id`` does not exist on the target session."""

    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"task not found: {task_id}")
        self.task_id = task_id


class TaskAlreadyDispatched(Exception):
    """Raised when a cancel is attempted on a task that is already running.

    v1 does not support cancelling an in-flight task; the HTTP route
    maps this to 409 (ADR-0009 Decision 6).
    """

    def __init__(self, task_id: UUID) -> None:
        super().__init__(f"task already dispatched: {task_id}")
        self.task_id = task_id


class SessionHasInFlightTask(Exception):
    """Raised when ``/messages`` is called while a queued task is dispatched.

    Per ADR-0009 Decision 6, ``/messages`` and the orchestration
    dispatcher are mutually exclusive on a session — at most one of
    ``(queue dispatch, messages dispatch)`` may be running at a time.
    The HTTP route maps this to 409 with the prescribed detail message.
    """

    def __init__(self, session_id: str, task_id: UUID) -> None:
        super().__init__(
            f"session {session_id} is running queued task {task_id};"
            f" wait or cancel via DELETE /tasks/{task_id}"
        )
        self.session_id = session_id
        self.task_id = task_id
