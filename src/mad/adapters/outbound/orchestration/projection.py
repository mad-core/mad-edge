"""In-memory projection of orchestration state derived from the event log.

The projection materialises ADR-0009 Decision 3: the JSONL event log
is the source of truth, and the per-session ``{queued, in_flight}``
state shown by ``GET /v1/sessions/{id}/tasks`` is a cache reconstructed
from that log.

Two entry points keep the cache fresh:

- ``bootstrap_from_log(log)`` — called once at startup. Replays every
  ``task.*`` event into the projection so a process restart preserves
  the queue.
- ``apply(event)`` — called on every event seen during normal
  operation. The composition root subscribes to the ``EventBus`` and
  feeds events here.

The projection implements the ``TaskQueue`` read port (``queued``,
``in_flight``); use cases consume it via that port and never reach
into the projection's mutators.
"""

from __future__ import annotations

from collections import defaultdict
from uuid import UUID

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_log_query import EventLogQuery, EventQuery
from mad.core.orchestration.domain.task import Task

_TASK_QUEUED = "task.queued"
_TASK_DISPATCHED = "task.dispatched"
_TERMINAL_TYPES = frozenset({"task.completed", "task.failed", "task.cancelled"})

# Bootstrap limit. EventLogQuery loads sessions/*.jsonl into memory;
# raising the limit just changes the in-memory slice, not I/O. v1
# deployments are nowhere near this; revisit when projection rebuild
# becomes a startup-latency concern (ADR-0009 Consequences).
_BOOTSTRAP_LIMIT = 1_000_000


class InMemoryTaskProjection:
    """Per-session ``{queued, in_flight}`` projection of ``task.*`` events."""

    def __init__(self) -> None:
        self._queued: dict[str, list[Task]] = defaultdict(list)
        self._in_flight: dict[str, Task] = {}

    # -- TaskQueue port ----------------------------------------------------

    def queued(self, session_id: str) -> list[Task]:
        return list(self._queued.get(session_id, []))

    def in_flight(self, session_id: str) -> Task | None:
        return self._in_flight.get(session_id)

    def pending_session_ids(self) -> list[str]:
        # Sorted so callers see a deterministic order regardless of
        # event arrival; defaultdict may hold empty lists after a
        # session's tasks all reach terminal state — filter those out.
        with_queued = {sid for sid, tasks in self._queued.items() if tasks}
        return sorted(with_queued | self._in_flight.keys())

    # -- Lifecycle ---------------------------------------------------------

    def bootstrap_from_log(self, log: EventLogQuery) -> None:
        """Replay every ``task.*`` event in the log into this projection."""
        events = log.query(EventQuery(limit=_BOOTSTRAP_LIMIT))
        for event in events:
            self.apply(event)

    def apply(self, event: Event) -> None:
        """Update state for a single event. Non-task events are ignored."""
        if event.type == _TASK_QUEUED:
            self._on_queued(event)
        elif event.type == _TASK_DISPATCHED:
            self._on_dispatched(event)
        elif event.type in _TERMINAL_TYPES:
            self._on_terminal(event)

    # -- Private mutators --------------------------------------------------

    def _on_queued(self, event: Event) -> None:
        task_id = UUID(event.data["task_id"])
        raw_mode = event.data.get("conversation_mode", "new")
        conversation_mode = raw_mode if raw_mode in ("new", "resume") else "new"
        task = Task(
            task_id=task_id,
            session_id=event.session_id,
            content=event.data["content"],
            scheduled_for=event.data["scheduled_for"],
            created_at=event.timestamp,
            model=event.data.get("model"),
            conversation_mode=conversation_mode,
        )
        self._queued[event.session_id].append(task)

    def _on_dispatched(self, event: Event) -> None:
        task_id = UUID(event.data["task_id"])
        queue = self._queued.get(event.session_id, [])
        for index, task in enumerate(queue):
            if task.task_id == task_id:
                self._in_flight[event.session_id] = queue.pop(index)
                return
        # Dispatched without a matching queued task: silently ignore.
        # This shouldn't happen in normal flow; if it does, the next
        # terminal event will tidy up via _on_terminal.

    def _on_terminal(self, event: Event) -> None:
        task_id = UUID(event.data["task_id"])
        in_flight = self._in_flight.get(event.session_id)
        if in_flight is not None and in_flight.task_id == task_id:
            del self._in_flight[event.session_id]
            return
        queue = self._queued.get(event.session_id, [])
        self._queued[event.session_id] = [t for t in queue if t.task_id != task_id]
