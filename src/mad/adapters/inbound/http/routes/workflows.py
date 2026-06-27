"""Workflow endpoints — sequential session chaining (issue #90, ADR-0013).

Two request/response routes:

- ``POST /v1/workflows`` — create a workflow from a DAG of steps. Each step is
  a session configuration plus an optional ``depends_on`` list; a step's task
  is held unqueued until **all** its predecessors emit ``task.completed``. A
  step's github mount may inherit a predecessor's repo via ``from_step``. A
  cyclic graph, an unknown ``depends_on``, or a dangling ``from_step`` is
  rejected with 422.
- ``GET /v1/workflows/{workflow_id}`` — workflow status
  (pending / running / completed / failed) plus per-step status.

All inputs and outputs are typed Pydantic models per hard rule 9. The
``WorkflowNotFound`` and ``InvalidWorkflow`` domain exceptions are mapped to
404 / 422 by app-level handlers — see ``mad.adapters.inbound.http.app``.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette import status

from mad.adapters.inbound.http.routes.sessions import AgentSpec
from mad.core.orchestration.domain.workflow import WorkflowMount, WorkflowStep
from mad.core.orchestration.use_cases.create_workflow import (
    CreateWorkflowInput,
    CreateWorkflowUseCase,
)
from mad.core.orchestration.use_cases.get_workflow import GetWorkflowUseCase

router = APIRouter(tags=["workflows"])


# -- Pydantic shapes -----------------------------------------------------------


class WorkflowMountRequest(BaseModel):
    mount_path: str = Field(..., description="Where the resource mounts in the workspace.")
    type: Literal["github_repository", "file"] = "github_repository"
    url: str | None = Field(
        default=None,
        description="Explicit github repo URL. Mutually exclusive with from_step.",
    )
    from_step: str | None = Field(
        default=None,
        description=(
            "Inherit this github mount's repo URL from a predecessor step and "
            "check out what it produced. Must be listed in the step's depends_on "
            "and reference a step that has a github mount, else 422."
        ),
    )
    ref: Literal["sha", "branch"] = Field(
        default="sha",
        description=(
            "For a from_step mount: 'sha' (default) pins the predecessor's "
            "immutable head_sha; 'branch' tracks its branch tip."
        ),
    )
    content: str = Field(default="", description="Inline content for a 'file' mount.")


class WorkflowStepSession(BaseModel):
    """A step's session configuration: the agent, the task prompt, and mounts."""

    agent: AgentSpec
    prompt: str = Field(..., description="Opaque task content forwarded verbatim to the launcher.")
    mounts: list[WorkflowMountRequest] = Field(default_factory=list)
    base_branch: str | None = None
    working_directory: str | None = None
    model: str | None = None
    effort: str | None = None
    timeout_s: float | None = Field(default=None, gt=0)


class WorkflowStepRequest(BaseModel):
    id: str = Field(..., description="Unique step id, referenced by depends_on / from_step.")
    depends_on: list[str] = Field(
        default_factory=list,
        description=(
            "Zero or more predecessor step ids. The step's task is not enqueued "
            "until all of them emit task.completed. An ordering barrier on its "
            "own — independent of from_step."
        ),
    )
    session: WorkflowStepSession


class CreateWorkflowRequest(BaseModel):
    steps: list[WorkflowStepRequest] = Field(..., min_length=1)


class CreateWorkflowResponse(BaseModel):
    workflow_id: str
    status: str = "pending"


class WorkflowStepStatusResponse(BaseModel):
    step_id: str
    status: Literal["pending", "running", "completed", "failed"]
    depends_on: list[str] = Field(default_factory=list)
    session_id: str | None = None
    reason: str | None = Field(default=None, description="Failure reason when status == 'failed'.")


class WorkflowStatusResponse(BaseModel):
    workflow_id: str
    status: Literal["pending", "running", "completed", "failed"]
    steps: list[WorkflowStepStatusResponse]


# -- Mapping -------------------------------------------------------------------


def _to_domain_step(step: WorkflowStepRequest) -> WorkflowStep:
    session = step.session
    mounts = tuple(
        WorkflowMount(
            mount_path=m.mount_path,
            type=m.type,
            url=m.url,
            from_step=m.from_step,
            ref=m.ref,
            content=m.content,
        )
        for m in session.mounts
    )
    return WorkflowStep(
        step_id=step.id,
        agent=session.agent.model_dump(),
        prompt=session.prompt,
        mounts=mounts,
        depends_on=tuple(step.depends_on),
        base_branch=session.base_branch,
        working_directory=session.working_directory,
        model=session.model,
        effort=session.effort,
        timeout_s=session.timeout_s,
    )


def _emitter(request: Request):
    return request.app.state.event_emitter


def _read_model(request: Request):
    return request.app.state.workflow_read_model


# -- Routes --------------------------------------------------------------------


@router.post(
    "/v1/workflows",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateWorkflowResponse,
)
async def create_workflow(
    payload: CreateWorkflowRequest, request: Request
) -> CreateWorkflowResponse:
    steps = tuple(_to_domain_step(s) for s in payload.steps)
    use_case = CreateWorkflowUseCase(emitter=_emitter(request))
    output = await use_case.execute(CreateWorkflowInput(steps=steps))
    return CreateWorkflowResponse(workflow_id=output.workflow_id, status=output.status)


@router.get(
    "/v1/workflows/{workflow_id}",
    response_model=WorkflowStatusResponse,
)
async def get_workflow(workflow_id: str, request: Request) -> WorkflowStatusResponse:
    use_case = GetWorkflowUseCase(read_model=_read_model(request))
    snapshot = use_case.execute(workflow_id)
    return WorkflowStatusResponse(
        workflow_id=snapshot.workflow_id,
        status=snapshot.status,  # type: ignore[arg-type]
        steps=[
            WorkflowStepStatusResponse(
                step_id=s.step_id,
                status=s.status,  # type: ignore[arg-type]
                depends_on=list(s.depends_on),
                session_id=s.session_id,
                reason=s.reason,
            )
            for s in snapshot.steps
        ],
    )
