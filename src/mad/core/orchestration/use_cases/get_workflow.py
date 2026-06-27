"""GetWorkflowUseCase — read a workflow's status and per-step status.

Reads the :class:`WorkflowReadModel` projection (rebuilt from the
``workflow.*`` event log) and raises :class:`WorkflowNotFound` (→ 404) for an
unknown id. Pure read — no events emitted.
"""

from __future__ import annotations

from mad.core.orchestration.domain.exceptions.workflow import WorkflowNotFound
from mad.core.orchestration.ports.workflow_read_model import (
    WorkflowReadModel,
    WorkflowSnapshot,
)


class GetWorkflowUseCase:
    """Return the snapshot for a workflow id, or raise ``WorkflowNotFound``."""

    def __init__(self, read_model: WorkflowReadModel) -> None:
        self._read_model = read_model

    def execute(self, workflow_id: str) -> WorkflowSnapshot:
        snapshot = self._read_model.get(workflow_id)
        if snapshot is None:
            raise WorkflowNotFound(workflow_id)
        return snapshot
