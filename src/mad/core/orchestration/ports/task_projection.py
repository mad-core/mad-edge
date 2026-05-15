"""TaskProjection port — the orchestration state writer the dispatcher tails.

The read-side ``TaskQueue`` port (sibling module) is the safe surface for
use cases that only need to inspect queue/in-flight state. The
dispatcher additionally needs to *advance* the projection as events
arrive on the bus; ``apply`` is that mutation hook.

Keeping ``apply`` separate from ``TaskQueue`` preserves the read/write
distinction at the type level while still letting the composition root
inject the same concrete object for both ports.
"""

from __future__ import annotations

from typing import Protocol

from mad.core.events.domain.event import Event
from mad.core.orchestration.ports.task_queue import TaskQueue


class TaskProjection(TaskQueue, Protocol):
    """Writable projection: ``TaskQueue`` reads plus an ``apply`` mutation hook."""

    def apply(self, event: Event) -> None:
        """Advance the projection with a single event from the bus."""
        ...
