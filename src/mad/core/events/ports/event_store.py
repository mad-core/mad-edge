"""EventStore port — the only write path to the session event log.

Together with EventBus, this is the surface that EventEmitter (the single
write gateway, CLAUDE.md hard rule 9 / ADR-0007) depends on. Use cases
never call the underlying SessionRepository.append_event directly.
"""

from __future__ import annotations

from typing import Any, Protocol

from mad.core.events.domain.event import Event


class EventStore(Protocol):
    """Append a single event to durable storage and return the typed Event."""

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event: ...
