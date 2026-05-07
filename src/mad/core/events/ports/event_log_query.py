"""EventLogQuery port — read-side queries over the persisted event log.

The events module reads from the same JSONL files that
``JsonlSessionRepository`` writes (CLAUDE.md hard rule 6). This port
exposes the read patterns the events use cases need:

- Paginated historical queries with the standard filter set.
- Last-Event-ID catch-up: events strictly after a given ``event_id``.
- ``?agent=<name>`` resolution: the set of ``session_id`` values whose
  ``session.created`` event named the requested agent (ADR-0004).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mad.core.events.domain.event import Event


@dataclass(frozen=True)
class EventQuery:
    """Filter set for paginated/historical reads.

    ``after_event_id`` carries the ``Last-Event-ID`` header value when
    a client reconnects to the SSE stream. ``limit`` is clamped by the
    use case to the per-endpoint maximum (1000 today).
    """

    session_id: str | None = None
    kind: str | None = None
    session_ids_for_agent: frozenset[str] | None = None
    since: datetime | None = None
    after_event_id: UUID | None = None
    limit: int = 100


class EventLogQuery(Protocol):
    """Read-only query over the persisted event log."""

    def query(self, q: EventQuery) -> Iterable[Event]:
        """Return matching events ordered by ``event_id`` ascending."""
        ...

    def session_ids_for_agent(self, agent_name: str) -> frozenset[str]:
        """Resolve ``?agent=<name>`` to a set of session ids."""
        ...
