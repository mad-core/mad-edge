"""Outbound port: SessionRepository.

Formal contract for persisting and reading session events.
The source of truth is the JSONL session log (CLAUDE.md hard rule 6).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionRepository(Protocol):
    """Append-only event log for a session.

    Implementations write events to durable storage (JSONL file, database,
    in-memory store for tests, etc.) and expose a read path for recovery.
    """

    def append_event(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def read_events(self, session_id: str) -> list[dict[str, Any]]: ...

    def exists(self, session_id: str) -> bool: ...
