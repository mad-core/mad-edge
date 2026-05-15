"""TaskQueue port — read-side projection of orchestration state per session.

Implementations replay the JSONL event log (ADR-0009 Decision 3) into
an in-memory projection: per session, a ``queued`` list (insertion
order) and at most one ``in_flight`` task. Terminal-state tasks
(completed, cancelled, failed) are not represented here; the event log
remains authoritative.

This is a **read** port. Use cases that mutate orchestration state
emit events through ``EventEmitter`` (hard rule 11); the projection
materialises those events on the next replay or, in a long-lived
process, by tailing the bus.
"""

from __future__ import annotations

from typing import Protocol

from mad.core.orchestration.domain.task import Task


class TaskQueue(Protocol):
    """Read-side projection of the orchestration state."""

    def queued(self, session_id: str) -> list[Task]:
        """Return the queued tasks for ``session_id`` in insertion order."""
        ...

    def in_flight(self, session_id: str) -> Task | None:
        """Return the currently-dispatched task for ``session_id``, if any."""
        ...
