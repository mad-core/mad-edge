"""Session endpoints — thin HTTP layer.

Each handler:
  1. Parses the HTTP request (JSON + headers).
  2. Instantiates the relevant use case with dependencies from app.state.
  3. Calls use_case.execute(input).
  4. Maps the result (or domain exception) to an HTTP response.

Business logic lives in mad.core.sessions.use_cases.*.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from mad.core.orchestration.use_cases.list_provider_models import ListProviderModelsUseCase
from mad.core.sessions import SessionStore
from mad.core.sessions.use_cases.cleanup_sessions import (
    CleanupSessionsInput,
    CleanupSessionsUseCase,
)
from mad.core.sessions.use_cases.create_session import (
    CreateSessionInput,
    CreateSessionUseCase,
    ResourceSpec,
)
from mad.core.sessions.use_cases.delete_session import DeleteSessionUseCase
from mad.core.sessions.use_cases.get_session import GetSessionUseCase
from mad.core.sessions.use_cases.list_sessions import (
    ListSessionsInput,
    ListSessionsUseCase,
)
from mad.core.sessions.use_cases.send_user_message import (
    SendUserMessageInput,
    SendUserMessageUseCase,
)

router = APIRouter(tags=["sessions"])


class AgentSpec(BaseModel):
    model_config = {"extra": "allow"}

    name: str = Field(..., description="Human-readable agent name.")
    provider: str = Field(
        ..., description="Launcher key, e.g. 'claude_cli'. Used to resolve AgentLauncher."
    )


class ResourceCheckout(BaseModel):
    branch: str | None = None
    ref: str | None = None


class ResourceRequest(BaseModel):
    type: Literal["github_repository", "file"]
    mount_path: str
    url: str = ""
    authorization_token: str | None = Field(
        default=None,
        deprecated=True,
        description=(
            "DEPRECATED (issue #89), removal target v0.6.0. Inline GitHub clone PAT. "
            "Source the credential from the host's GITHUB_TOKEN (or GH_TOKEN) "
            "environment variable instead — passing a secret through the request body "
            "risks leaking it into client logs / MCP transcripts. When still supplied, "
            "it takes precedence over the host env var and emits a deprecation warning."
        ),
    )
    checkout: ResourceCheckout | dict[str, Any] | None = None
    content: str = ""


class CreateSessionRequest(BaseModel):
    agent: AgentSpec
    resources: list[ResourceRequest] = Field(default_factory=list)
    base_branch: str | None = None
    working_directory: str | None = Field(
        default=None,
        description=(
            "Optional /workspace/... path to use as the agent's working directory. "
            "When unset and exactly one github_repository resource is present, the "
            "directory auto-derives from that mount; otherwise it falls back to the "
            "workspace root."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Optional model id; overrides the deployment default for this session. "
            "``null`` (default) inherits from the deployment model default."
        ),
    )
    effort: str | None = Field(
        default=None,
        description=(
            "Optional reasoning-effort level; overrides the deployment default for "
            "this session. Forwarded verbatim to the provider CLI (``--effort`` for "
            "claude, ``--variant`` for opencode) — an opaque pass-through string, not "
            "validated by Mad. ``null`` (default) inherits from the deployment effort "
            "default."
        ),
    )
    timeout_s: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Optional per-session launcher timeout in seconds; overrides the operator "
            "default for this session. Resolution order: this value > the "
            "``MAD_AGENT_TIMEOUT_S`` env var > 600 s. ``null`` (default) inherits the "
            "operator default. Must be greater than 0."
        ),
    )
    auto_sync: bool | None = Field(
        default=None,
        description=(
            "Optional per-session toggle for the post-run auto-sync step, which "
            "publishes any leftover uncommitted work to a `mad/<session_id>` branch "
            "and opens a PR. Off by default. Set `true` to opt in when this session "
            "wants that safety net; leave it off when the session's tasks manage "
            "their own named branch/PR (auto-sync cannot see that branch and would "
            "open a duplicate PR next to it). Resolution order: per-task `auto_sync` "
            "> this value > the `MAD_AUTO_SYNC` env var > `false`. `null` (default) "
            "inherits the operator default."
        ),
    )


class SendMessageRequest(BaseModel):
    content: str


class SessionSummaryResponse(BaseModel):
    session_id: str
    status: str
    priority: int
    created_at: datetime
    updated_at: datetime


class SessionDetailResponse(BaseModel):
    session_id: str
    status: str
    workspace: str
    events: list[dict[str, Any]]
    priority: int
    created_at: datetime
    updated_at: datetime
    last_conversation_id: str | None = None


class CleanupSessionsRequest(BaseModel):
    older_than: datetime = Field(
        ...,
        description=(
            "Tz-aware ISO 8601 datetime. Sessions whose updated_at is strictly less "
            "than this value (and whose status is not already 'deleted') are eligible "
            "for cleanup. Future values return 400."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, the response reports the would-be-deleted ids in `would_delete` "
            "without destroying workspaces or emitting session.deleted events."
        ),
    )


class CleanupSessionsResponse(BaseModel):
    deleted_session_ids: list[str] = Field(default_factory=list)
    would_delete: list[str] = Field(default_factory=list)
    examined: int = 0


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize a query datetime to tz-aware UTC.

    FastAPI accepts both date-only (``2026-05-01``) and naive datetime
    (``2026-05-01T00:00:00``) values for a ``datetime`` query param. Both
    arrive without ``tzinfo``; comparing them against the tz-aware
    ``Session.created_at`` raises ``TypeError`` ("can't compare offset-naive
    and offset-aware datetimes"), surfacing as a 500. We assume UTC for
    naive inputs — the documented timezone of the persisted timestamps.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _store(request: Request) -> SessionStore:
    return request.app.state.store


def _repo(request: Request):
    return request.app.state.session_repo


def _provisioner(request: Request):
    return request.app.state.workspace_provisioner


@router.post("/v1/sessions")
async def create_session(
    payload: CreateSessionRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    store = _store(request)

    # 422 validation: if a model is specified, verify it exists in the catalog.
    if payload.model is not None:
        await ListProviderModelsUseCase(catalog=request.app.state.model_catalog).validate_model(
            payload.agent.provider, payload.model
        )

    resource_specs = [
        ResourceSpec(
            type=r.type,
            mount_path=r.mount_path,
            url=r.url,
            authorization_token=r.authorization_token,
            checkout=(
                r.checkout.model_dump(exclude_none=True)
                if isinstance(r.checkout, ResourceCheckout)
                else r.checkout
            ),
            content=r.content,
        )
        for r in payload.resources
    ]

    use_case = CreateSessionUseCase(
        provisioner=_provisioner(request),
        sessions_index=store.sessions,
        idempotency_index=store.idempotency,
        emitter=request.app.state.event_emitter,
    )

    output = await use_case.execute(
        CreateSessionInput(
            agent=payload.agent.model_dump(),
            resources=resource_specs,
            idempotency_key=idempotency_key,
            base_branch=payload.base_branch,
            working_directory=payload.working_directory,
            model=payload.model,
            effort=payload.effort,
            timeout_s=payload.timeout_s,
            auto_sync=payload.auto_sync,
        )
    )

    return output.session.response


@router.post("/v1/sessions/{session_id}/messages")
async def send_message(session_id: str, payload: SendMessageRequest, request: Request) -> dict:
    store = _store(request)

    use_case = SendUserMessageUseCase(
        sessions_index=store.sessions,
        get_launcher=request.app.state.launcher_factory,
        emitter=request.app.state.event_emitter,
        task_queue=request.app.state.task_projection,
        deployment_model_config=getattr(request.app.state, "deployment_model_config", None),
        deployment_effort_config=getattr(request.app.state, "deployment_effort_config", None),
    )
    use_case.execute(SendUserMessageInput(session_id=session_id, content=payload.content))

    return {"status": "accepted"}


@router.get("/v1/sessions/{session_id}", response_model=SessionDetailResponse)
async def get_session(session_id: str, request: Request) -> SessionDetailResponse:
    store = _store(request)

    use_case = GetSessionUseCase(
        repo=_repo(request),
        sessions_index=store.sessions,
    )
    output = use_case.execute(session_id)

    return SessionDetailResponse(
        session_id=output.session_id,
        status=output.status,
        workspace=output.workspace,
        events=output.events,
        priority=output.priority,
        created_at=output.created_at,
        updated_at=output.updated_at,
        last_conversation_id=output.last_conversation_id,
    )


@router.get("/v1/sessions", response_model=list[SessionSummaryResponse])
async def list_sessions(
    request: Request,
    created_after: Annotated[datetime | None, Query()] = None,
    created_before: Annotated[datetime | None, Query()] = None,
    updated_after: Annotated[datetime | None, Query()] = None,
    updated_before: Annotated[datetime | None, Query()] = None,
    order_by: Annotated[Literal["created_at", "updated_at"] | None, Query()] = None,
    order: Annotated[Literal["asc", "desc"], Query()] = "asc",
    include_deleted: Annotated[bool, Query()] = False,
) -> list[SessionSummaryResponse]:
    store = _store(request)

    use_case = ListSessionsUseCase(
        sessions_index=store.sessions,
        repo=_repo(request),
    )
    output = use_case.execute(
        ListSessionsInput(
            created_after=_as_utc(created_after),
            created_before=_as_utc(created_before),
            updated_after=_as_utc(updated_after),
            updated_before=_as_utc(updated_before),
            order_by=order_by,
            order=order,
            include_deleted=include_deleted,
        )
    )
    return [
        SessionSummaryResponse(
            session_id=s.session_id,
            status=s.status,
            priority=s.priority,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in output.sessions
    ]


@router.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict:
    store = _store(request)

    use_case = DeleteSessionUseCase(
        provisioner=_provisioner(request),
        sessions_index=store.sessions,
        emitter=request.app.state.event_emitter,
        task_queue=request.app.state.task_projection,
    )
    output = await use_case.execute(session_id)
    return {"status": output.status, "session_id": output.session_id}


@router.post("/v1/sessions/cleanup", response_model=CleanupSessionsResponse)
async def cleanup_sessions(
    payload: CleanupSessionsRequest, request: Request
) -> CleanupSessionsResponse:
    cutoff = _as_utc(payload.older_than)
    assert cutoff is not None  # mandatory field; Pydantic guards None
    if cutoff > datetime.now(UTC):
        raise HTTPException(status_code=400, detail="older_than is not valid")

    store = _store(request)
    use_case = CleanupSessionsUseCase(
        provisioner=_provisioner(request),
        sessions_index=store.sessions,
        repo=_repo(request),
        emitter=request.app.state.event_emitter,
        task_queue=request.app.state.task_projection,
    )
    output = await use_case.execute(
        CleanupSessionsInput(older_than=cutoff, dry_run=payload.dry_run)
    )
    return CleanupSessionsResponse(
        deleted_session_ids=output.deleted_session_ids,
        would_delete=output.would_delete,
        examined=output.examined,
    )
