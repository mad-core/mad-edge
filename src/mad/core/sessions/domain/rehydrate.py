"""Rehydrate a Session entity from its persisted JSONL events.

Pure domain helper — no I/O, no port dependencies. Callers read the
events from a SessionRepository and pass them in. Used by GetSession
and ListSessions to recover sessions that are not in the in-memory
index (hard rule 6: JSONL is the source of truth).
"""
from __future__ import annotations

from typing import Any

from mad.core.sessions.domain.entities.session import Session


def rehydrate_from_events(session_id: str, events: list[dict[str, Any]]) -> Session:
    """Build a minimal Session entity from its persisted event stream."""
    agent: dict[str, Any] = {}
    workspace = ""
    status = "created"

    for event in events:
        etype = event.get("type", "")
        if etype == "session.created":
            agent = {"name": event.get("agent", ""), "provider": "unknown"}
        elif etype == "session.status_running":
            status = "running"
        elif etype == "session.status_idle":
            status = "idle"
        elif etype == "session.error":
            status = "error"
        elif etype == "session.deleted":
            status = "deleted"

    return Session(
        session_id=session_id,
        agent=agent,
        workspace=workspace,
        status=status,
    )
