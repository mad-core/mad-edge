"""Rehydrate sessions with pending work into the live index at startup.

Issue #46 Part A — repairs the #28 foundation. The app lifespan
bootstraps the task projection from the JSONL log, but nothing ever
repopulated ``store.sessions``: after any restart the dispatcher saw an
empty index and queued work silently never resumed (and orphan
recovery, which walks the index, never fired). This use case closes
that gap by rebuilding ONLY the sessions the projection says have
pending work — queued or in-flight tasks — via the existing
``rehydrate_from_events`` replay. No parallel store; the JSONL log
stays the sole source of truth (hard rule 6).

Called from the app lifespan after ``bootstrap_from_log`` and before
``dispatcher.start()``.
"""

from __future__ import annotations

from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.rehydrate import rehydrate_from_events
from mad.core.sessions.ports.outbound.session_repository import SessionRepository


def rehydrate_pending_sessions(
    projection: TaskQueue,
    repo: SessionRepository,
    sessions_index: dict[str, Session],
) -> list[str]:
    """Insert every pending-work session missing from ``sessions_index``.

    Returns the ids that were rehydrated. Sessions already live in the
    index are left untouched (they are fresher than any replay).
    """
    rehydrated: list[str] = []
    for session_id in projection.pending_session_ids():
        if session_id in sessions_index:
            continue
        if not repo.exists(session_id):
            # The projection was built from the same JSONL log this repo
            # reads — a pending session without a log is an invariant
            # violation, not a recoverable state (hard rule 7).
            raise RuntimeError(
                f"session {session_id!r} has pending tasks in the projection "
                "but no persisted event log to rehydrate from"
            )
        sessions_index[session_id] = rehydrate_from_events(session_id, repo.read_events(session_id))
        rehydrated.append(session_id)
    return rehydrated
