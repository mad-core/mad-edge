"""Orchestration endpoints — task queue per session (ADR-0009 / issue #28).

Three endpoints:
- ``POST /v1/sessions/{session_id}/tasks`` — enqueue a task; emits
  ``task.queued``. Returns 202 with the new task id and status.
- ``GET /v1/sessions/{session_id}/tasks`` — return the per-session
  ``{queued, in_flight}`` projection.
- ``DELETE /v1/sessions/{session_id}/tasks/{task_id}`` — cancel a queued
  task; emits ``task.cancelled``. 404 if the task is not on the queue,
  409 if it is already dispatched (ADR-0009 Decision 6).

All inputs and outputs are typed Pydantic models per hard rule 9. The
domain exceptions raised by the use cases (``SessionNotFound``,
``TaskNotFound``, ``TaskAlreadyDispatched``) are mapped to status codes
by app-level exception handlers — see ``mad.adapters.inbound.http.app``.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette import status

from mad.core.orchestration.use_cases.cancel_task import (
    CancelTaskInput,
    CancelTaskUseCase,
)
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.orchestration.use_cases.list_tasks import ListTasksUseCase

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
