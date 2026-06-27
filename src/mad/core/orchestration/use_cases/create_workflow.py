"""CreateWorkflowUseCase — validate a workflow graph and emit ``workflow.created``.

The use case validates the DAG (raising :class:`InvalidWorkflow` → 422 for a
cyclic graph, an unknown ``depends_on``, or a dangling ``from_step``) and
records the graph as a single ``workflow.created`` event under a reserved
``workflow_id`` stream. Per hard rule 11 the event goes through
``EventEmitter.emit()``.

It does NOT start any step. The ``WorkflowCoordinator``
reacts to ``workflow.created`` on the bus and starts the root steps — so
creation is a fast, side-effect-light intake and all provisioning/dispatch
logic lives in one place (the coordinator), exactly as ``EnqueueTaskUseCase``
is intake-only and the dispatcher owns dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.workflow import (
    WorkflowStep,
    validate_workflow,
    workflow_to_created_data,
)


@dataclass(frozen=True)
class CreateWorkflowInput:
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True)
class CreateWorkflowOutput:
    workflow_id: str
    status: str


class CreateWorkflowUseCase:
    """Validate and persist a workflow; the coordinator drives execution."""

    def __init__(self, emitter: EventEmitter) -> None:
        self._emitter = emitter

    async def execute(self, payload: CreateWorkflowInput) -> CreateWorkflowOutput:
        # 422 on a structurally invalid graph BEFORE persisting anything.
        validate_workflow(payload.steps)

        workflow_id = "wkfl_" + uuid4().hex[:12]
        await self._emitter.emit(
            workflow_id,
            "workflow.created",
            workflow_to_created_data(workflow_id, payload.steps),
        )
        return CreateWorkflowOutput(workflow_id=workflow_id, status="pending")
