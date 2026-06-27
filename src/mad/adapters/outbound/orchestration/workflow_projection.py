"""In-memory projection of workflow state from the ``workflow.*`` event log.

Mirrors :mod:`mad.adapters.outbound.orchestration.projection` (the task
projection): the JSONL event log is the source of truth (hard rule 6) and the
per-workflow status shown by ``GET /v1/workflows/{id}`` is a cache rebuilt
from ``workflow.*`` events.

Two entry points keep the cache fresh: ``bootstrap_from_log`` replays every
``workflow.*`` event once at startup, and ``apply`` is fed each event the
coordinator's loop observes during normal operation. The projection
implements the ``WorkflowReadModel`` read port; the ``GetWorkflowUseCase``
consumes it via that port.
"""

from __future__ import annotations

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_log_query import EventLogQuery, EventQuery
from mad.core.orchestration.domain.workflow import (
    derive_step_status,
    derive_workflow_status,
    steps_from_created_data,
)
from mad.core.orchestration.ports.workflow_read_model import (
    StepSnapshot,
    WorkflowSnapshot,
)

_WORKFLOW_CREATED = "workflow.created"
_STEP_STARTED = "workflow.step.started"
_STEP_COMPLETED = "workflow.step.completed"
_STEP_FAILED = "workflow.step.failed"

# Matches the task projection bootstrap limit (ADR-0009 Consequences): the
# query loads sessions/*.jsonl into memory, so the limit only bounds the
# in-memory slice, not I/O.
_BOOTSTRAP_LIMIT = 1_000_000


class _StepState:
    __slots__ = ("completed", "depends_on", "failed", "reason", "session_id", "started")

    def __init__(self, depends_on: tuple[str, ...]) -> None:
        self.depends_on = depends_on
        self.started = False
        self.completed = False
        self.failed = False
        self.session_id: str | None = None
        self.reason: str | None = None


class _WorkflowState:
    def __init__(self, order: list[str], steps: dict[str, _StepState]) -> None:
        self.order = order
        self.steps = steps


class InMemoryWorkflowProjection:
    """Per-workflow ``{step -> status}`` projection of ``workflow.*`` events."""

    def __init__(self) -> None:
        self._workflows: dict[str, _WorkflowState] = {}

    # -- WorkflowReadModel port --------------------------------------------

    def get(self, workflow_id: str) -> WorkflowSnapshot | None:
        state = self._workflows.get(workflow_id)
        if state is None:
            return None
        steps = [
            StepSnapshot(
                step_id=step_id,
                status=derive_step_status(
                    started=st.started, completed=st.completed, failed=st.failed
                ),
                depends_on=st.depends_on,
                session_id=st.session_id,
                reason=st.reason,
            )
            for step_id, st in ((sid, state.steps[sid]) for sid in state.order)
        ]
        status = derive_workflow_status({s.step_id: s.status for s in steps})
        return WorkflowSnapshot(workflow_id=workflow_id, status=status, steps=steps)

    # -- Lifecycle ---------------------------------------------------------

    def bootstrap_from_log(self, log: EventLogQuery) -> None:
        for event in log.query(EventQuery(limit=_BOOTSTRAP_LIMIT)):
            self.apply(event)

    def apply(self, event: Event) -> None:
        if event.type == _WORKFLOW_CREATED:
            self._on_created(event)
        elif event.type == _STEP_STARTED:
            self._on_started(event)
        elif event.type == _STEP_COMPLETED:
            self._on_step_terminal(event, failed=False)
        elif event.type == _STEP_FAILED:
            self._on_step_terminal(event, failed=True)

    # -- Private mutators --------------------------------------------------

    def _on_created(self, event: Event) -> None:
        workflow_id = event.data["workflow_id"]
        steps = steps_from_created_data(event.data)
        order = [s.step_id for s in steps]
        step_states = {s.step_id: _StepState(tuple(s.depends_on)) for s in steps}
        self._workflows[workflow_id] = _WorkflowState(order, step_states)

    def _on_started(self, event: Event) -> None:
        state = self._workflows.get(event.session_id)
        if state is None:
            return
        step = state.steps.get(event.data["step_id"])
        if step is None:
            return
        step.started = True
        step.session_id = event.data.get("session_id")

    def _on_step_terminal(self, event: Event, *, failed: bool) -> None:
        state = self._workflows.get(event.session_id)
        if state is None:
            return
        step = state.steps.get(event.data["step_id"])
        if step is None:
            return
        if failed:
            step.failed = True
            step.reason = event.data.get("reason")
        else:
            step.completed = True
