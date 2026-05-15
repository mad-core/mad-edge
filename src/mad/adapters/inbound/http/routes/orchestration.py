"""Orchestration endpoints — task queue + dispatch policies (ADR-0009).

Five endpoints:

Task queue (issue #28):
- ``POST /v1/sessions/{session_id}/tasks`` — enqueue a task.
- ``GET /v1/sessions/{session_id}/tasks`` — list queued + in-flight.
- ``DELETE /v1/sessions/{session_id}/tasks/{task_id}`` — cancel a queued task.

Dispatch policies (issue #33 / ADR-0009 §9):
- ``PATCH /v1/sessions/{session_id}/dispatch_policy`` — set the policy
  (``immediate`` / ``work_window`` / ``manual``); emits
  ``dispatch_policy.updated``.
- ``POST /v1/sessions/{session_id}/dispatch_policy/trigger`` — drain the
  queue once in ``manual`` mode; 409 in any other mode.

All inputs and outputs are typed Pydantic models per hard rule 9.
Domain exceptions raised by the use cases (``SessionNotFound``,
``TaskNotFound``, ``TaskAlreadyDispatched``, ``InvalidDispatchPolicy``,
``TriggerNotApplicable``) are mapped to status codes by app-level
handlers — see ``mad.adapters.inbound.http.app``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette import status

from mad.core.orchestration.domain.dispatch_policy import (
    InvalidDispatchPolicy,
    policy_from_dict,
    policy_to_dict,
)
from mad.core.orchestration.use_cases.cancel_task import (
    CancelTaskInput,
    CancelTaskUseCase,
)
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.orchestration.use_cases.list_tasks import ListTasksUseCase
from mad.core.orchestration.use_cases.trigger_manual_dispatch import (
    TriggerManualDispatchInput,
    TriggerManualDispatchUseCase,
)
from mad.core.orchestration.use_cases.update_dispatch_policy import (
    UpdateDispatchPolicyInput,
    UpdateDispatchPolicyUseCase,
)

router = APIRouter(tags=["orchestration"])


# -- Pydantic shapes -----------------------------------------------------------


class EnqueueTaskRequest(BaseModel):
    content: str = Field(..., description="Opaque payload forwarded verbatim to the launcher.")
    scheduled_for: str = Field(
        default="now",
        description=(
            "Scheduling hint. ``now`` (default) preserves the immediate-dispatch"
            " behaviour. Other values (``next_window``, ISO 8601 timestamps) are"
            " accepted but not interpreted in v1 — recorded on the event verbatim."
        ),
    )


class EnqueueTaskResponse(BaseModel):
    task_id: UUID
    session_id: str
    scheduled_for: str
    status: str = "queued"


class TaskResponse(BaseModel):
    task_id: UUID
    session_id: str
    content: str
    scheduled_for: str
    created_at: datetime


class ListTasksResponse(BaseModel):
    queued: list[TaskResponse]
    in_flight: TaskResponse | None = None


class CancelTaskResponse(BaseModel):
    status: str = "cancelled"
    task_id: UUID


# -- Dispatch policy shapes (issue #33) ----------------------------------------


class WindowSpec(BaseModel):
    """One time window inside a ``work_window`` policy.

    ``start`` / ``end`` are HH:MM 24h literals. Windows can wrap midnight
    (``end <= start``). ``timezone`` is an IANA name so DST is honored.
    ``days`` is optional; defaults to all days.
    """

    start: str = Field(..., description="HH:MM 24h, e.g. '18:00'.")
    end: str = Field(..., description="HH:MM 24h, e.g. '08:00'. Wraps midnight if <= start.")
    timezone: str = Field(..., description="IANA timezone, e.g. 'America/Mexico_City'.")
    days: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] | None = Field(
        default=None,
        description="Optional weekday filter; defaults to every day.",
    )


class ImmediatePolicyRequest(BaseModel):
    kind: Literal["immediate"]


class WorkWindowPolicyRequest(BaseModel):
    kind: Literal["work_window"]
    windows: list[WindowSpec] = Field(..., min_length=1)


class ManualPolicyRequest(BaseModel):
    kind: Literal["manual"]


DispatchPolicyRequest = Annotated[
    ImmediatePolicyRequest | WorkWindowPolicyRequest | ManualPolicyRequest,
    Field(discriminator="kind"),
]


class DispatchPolicyResponse(BaseModel):
    """Echoed canonical policy after a successful PATCH."""

    session_id: str
    policy: dict[str, Any]


class TriggerManualDispatchResponse(BaseModel):
    session_id: str
    drained: int = Field(..., description="Number of currently-queued tasks the trigger covers.")


# -- Helpers -------------------------------------------------------------------


def _store(request: Request):
    return request.app.state.store


def _projection(request: Request):
    return request.app.state.task_projection


def _emitter(request: Request):
    return request.app.state.event_emitter


# -- Routes --------------------------------------------------------------------


@router.post(
    "/v1/sessions/{session_id}/tasks",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=EnqueueTaskResponse,
)
async def enqueue_task(
    session_id: str,
    payload: EnqueueTaskRequest,
    request: Request,
) -> EnqueueTaskResponse:
    use_case = EnqueueTaskUseCase(
        sessions_index=_store(request).sessions,
        emitter=_emitter(request),
    )
    output = await use_case.execute(
        EnqueueTaskInput(
            session_id=session_id,
            content=payload.content,
            scheduled_for=payload.scheduled_for,
        )
    )
    return EnqueueTaskResponse(
        task_id=output.task_id,
        session_id=output.session_id,
        scheduled_for=output.scheduled_for,
    )


@router.get(
    "/v1/sessions/{session_id}/tasks",
    response_model=ListTasksResponse,
)
async def list_tasks(session_id: str, request: Request) -> ListTasksResponse:
    use_case = ListTasksUseCase(
        sessions_index=_store(request).sessions,
        task_queue=_projection(request),
    )
    output = use_case.execute(session_id)
    return ListTasksResponse(
        queued=[
            TaskResponse(
                task_id=t.task_id,
                session_id=t.session_id,
                content=t.content,
                scheduled_for=t.scheduled_for,
                created_at=t.created_at,
            )
            for t in output.queued
        ],
        in_flight=(
            TaskResponse(
                task_id=output.in_flight.task_id,
                session_id=output.in_flight.session_id,
                content=output.in_flight.content,
                scheduled_for=output.in_flight.scheduled_for,
                created_at=output.in_flight.created_at,
            )
            if output.in_flight is not None
            else None
        ),
    )


@router.delete(
    "/v1/sessions/{session_id}/tasks/{task_id}",
    response_model=CancelTaskResponse,
)
async def cancel_task(
    session_id: str,
    task_id: UUID,
    request: Request,
) -> CancelTaskResponse:
    use_case = CancelTaskUseCase(
        sessions_index=_store(request).sessions,
        task_queue=_projection(request),
        emitter=_emitter(request),
    )
    await use_case.execute(CancelTaskInput(session_id=session_id, task_id=task_id))
    return CancelTaskResponse(task_id=task_id)


# -- Dispatch policy routes (issue #33) ---------------------------------------


@router.patch(
    "/v1/sessions/{session_id}/dispatch_policy",
    response_model=DispatchPolicyResponse,
)
async def update_dispatch_policy(
    session_id: str,
    payload: DispatchPolicyRequest,
    request: Request,
) -> DispatchPolicyResponse:
    """Set the dispatch policy for a session.

    The body is a discriminated union on ``kind``:
    - ``{"kind": "immediate"}`` (default — same as today)
    - ``{"kind": "work_window", "windows": [...]}`` (overnight runs etc.)
    - ``{"kind": "manual"}`` (queue accumulates; explicit trigger drains)

    Emits ``dispatch_policy.updated`` so the policy survives a process
    restart via JSONL replay.
    """
    policy_dict = payload.model_dump()
    try:
        policy = policy_from_dict(policy_dict)
    except InvalidDispatchPolicy:
        # Pydantic should have caught most malformed input; this catches
        # edge cases like an unknown IANA timezone that only the domain
        # validator can reject.
        raise

    use_case = UpdateDispatchPolicyUseCase(
        sessions_index=_store(request).sessions,
        emitter=_emitter(request),
    )
    output = await use_case.execute(UpdateDispatchPolicyInput(session_id=session_id, policy=policy))
    return DispatchPolicyResponse(
        session_id=output.session_id,
        policy=policy_to_dict(output.policy),
    )


@router.post(
    "/v1/sessions/{session_id}/dispatch_policy/trigger",
    response_model=TriggerManualDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_manual_dispatch(
    session_id: str,
    request: Request,
) -> TriggerManualDispatchResponse:
    """Drain the current queue once. Manual mode only; 409 otherwise."""
    use_case = TriggerManualDispatchUseCase(
        sessions_index=_store(request).sessions,
        task_queue=_projection(request),
    )
    output = use_case.execute(TriggerManualDispatchInput(session_id=session_id))
    return TriggerManualDispatchResponse(session_id=output.session_id, drained=output.drained)
