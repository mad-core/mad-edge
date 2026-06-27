"""WorkflowReadModel port — read-side projection of workflow status (issue #90).

``GET /v1/workflows/{id}`` reads workflow + per-step status from a projection
reconstructed from the ``workflow.*`` event stream (ADR-0013, mirroring the
task ``TaskQueue`` projection). This is a **read** port; the workflow
coordinator is the only writer of ``workflow.*`` events, via ``EventEmitter``
(hard rule 11). ``apply`` advances the projection as those events arrive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from mad.core.events.domain.event import Event


@dataclass(frozen=True)
class StepSnapshot:
    """One step's status in a workflow snapshot."""

    step_id: str
    status: str
    depends_on: tuple[str, ...] = ()
    session_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class WorkflowSnapshot:
    """Status of a whole workflow and each of its steps."""

    workflow_id: str
    status: str
    steps: list[StepSnapshot] = field(default_factory=list)


class WorkflowReadModel(Protocol):
    """Read-side projection of workflow state."""

    def get(self, workflow_id: str) -> WorkflowSnapshot | None:
        """Return the snapshot for ``workflow_id``, or ``None`` if unknown."""
        ...

    def apply(self, event: Event) -> None:
        """Advance the projection with a single ``workflow.*`` event."""
        ...
