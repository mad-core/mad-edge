"""Task domain entity for the orchestration module.

A ``Task`` is a unit of work submitted via ``POST /v1/sessions/{id}/tasks``.
Tasks are **opaque content** (ADR-0009 Decision 7 / hard rule 1) — the
``content`` field is a free-form string the orchestration module never
inspects. The launcher receives it verbatim when the task is dispatched.

State is **not** carried on the entity. The projection (ADR-0009
Decision 3) places a ``Task`` in either the per-session ``queued`` list
or as the ``in_flight`` slot; terminal-state tasks (completed,
cancelled, failed) leave the projection. The full state history lives
in the JSONL event log as ``task.queued`` → ``task.dispatched`` →
``task.{completed,cancelled,failed}``.

``scheduled_for`` is recorded as a free-form string in v1: ``"now"``,
``"next_window"``, or an ISO 8601 timestamp. The HTTP layer validates
the shape; the domain stores the value verbatim. v1 has no scheduling
behaviour — that's the next issue. Recording the value now means the
next issue is purely additive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class Task:
    """A unit of orchestrated work attached to a session."""

    task_id: UUID
    session_id: str
    content: str
    scheduled_for: str
    created_at: datetime
    model: str | None = None
    conversation_mode: Literal["new", "resume"] = "new"
