"""FastMCP server exposing Mad's HTTP surface as MCP tools.

One MCP tool per request/response HTTP route (CLAUDE.md hard rule 13,
ADR-0012). Each tool instantiates the same use case the HTTP handler
uses, against the same in-process dependencies (store / repo /
provisioner / emitter / launcher factory / deployment policy / event
log), and returns the same Pydantic shapes the HTTP layer returns — so
the MCP boundary cannot drift from the REST boundary by construction
(hard rule 9). ``tests/integration/api/test_http_mcp_parity.py`` fails
if a JSON route is added without its tool.

The ONLY HTTP route deliberately NOT mirrored is the streaming SSE
surface ``GET /v1/events/stream`` — server-sent events are operator
telemetry, not a request/response tool, and stay on their own MCP
surface (hard rule 13 carve-out, issue #32, ADR-0004). The historical
query ``GET /v1/events`` IS exposed as ``mad_query_events``.

Classification ("which failed / needs attention / can I delete") belongs
to the orchestrator LLM reading the tool results; Mad returns raw status
and infers nothing (hard rule 1).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mad.adapters.inbound.http.routes.config import (
    ConfigResponse,
    build_config_response,
)
from mad.adapters.inbound.http.routes.events import _serialize_event
from mad.adapters.inbound.http.routes.orchestration import (
    CancelTaskResponse,
    ClearDispatchPolicyResponse,
    DeploymentDispatchPolicyResponse,
    DispatchPolicyRequest,
    DispatchPolicyResponse,
    EnqueueTaskRequest,
    EnqueueTaskResponse,
    GlobalQueueResponse,
    ListTasksResponse,
    SessionPriorityResponse,
    TaskResponse,
    TriggerManualDispatchResponse,
    UpdatePriorityRequest,
    _queue_task_entry,
    _retry_info_response,
    _scheduled_task_entry,
)
from mad.adapters.inbound.http.routes.providers import (
    DeploymentEffortResponse,
    DeploymentModelResponse,
    ProviderModelsResponse,
    SetDeploymentEffortRequest,
    SetDeploymentModelRequest,
)
from mad.adapters.inbound.http.routes.sessions import (
    CleanupSessionsRequest,
    CleanupSessionsResponse,
    CreateSessionRequest,
    SendMessageRequest,
    SessionDetailResponse,
    SessionSummaryResponse,
    _as_utc,
)
from mad.adapters.inbound.http.routes.workflows import (
    CreateWorkflowRequest,
    CreateWorkflowResponse,
    WorkflowStatusResponse,
    WorkflowStepStatusResponse,
    _to_domain_step,
)
from mad.core.config.settings import load_settings
from mad.core.config.use_cases.get_config import GetConfigUseCase
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.events.use_cases.query_events import QueryEventsInput, QueryEventsUseCase
from mad.core.orchestration.domain.deployment_policy import DeploymentDispatchPolicy
from mad.core.orchestration.domain.dispatch_policy import policy_from_dict, policy_to_dict
from mad.core.orchestration.domain.effort_config import DeploymentEffortConfig
from mad.core.orchestration.domain.model_config import DeploymentModelConfig
from mad.core.orchestration.ports.clock import Clock
from mad.core.orchestration.ports.model_catalog import ModelCatalog
from mad.core.orchestration.use_cases.cancel_task import CancelTaskInput, CancelTaskUseCase
from mad.core.orchestration.use_cases.clear_dispatch_policy import (
    ClearDispatchPolicyInput,
    ClearDispatchPolicyUseCase,
)
from mad.core.orchestration.use_cases.create_workflow import (
    CreateWorkflowInput,
    CreateWorkflowUseCase,
)
from mad.core.orchestration.use_cases.deployment_dispatch_policy import (
    GetDeploymentDispatchPolicyUseCase,
    SetDeploymentDispatchPolicyInput,
    SetDeploymentDispatchPolicyUseCase,
)
from mad.core.orchestration.use_cases.deployment_effort_config import (
    ClearDeploymentEffortUseCase,
    DeploymentEffortOutput,
    GetDeploymentEffortUseCase,
    SetDeploymentEffortInput,
    SetDeploymentEffortUseCase,
)
from mad.core.orchestration.use_cases.deployment_model_config import (
    ClearDeploymentModelUseCase,
    DeploymentModelOutput,
    GetDeploymentModelUseCase,
    SetDeploymentModelInput,
    SetDeploymentModelUseCase,
)
from mad.core.orchestration.use_cases.enqueue_task import EnqueueTaskInput, EnqueueTaskUseCase
from mad.core.orchestration.use_cases.get_global_queue import GetGlobalQueueUseCase
from mad.core.orchestration.use_cases.get_workflow import GetWorkflowUseCase
from mad.core.orchestration.use_cases.list_provider_models import ListProviderModelsUseCase
from mad.core.orchestration.use_cases.list_tasks import ListTasksUseCase
from mad.core.orchestration.use_cases.trigger_manual_dispatch import (
    TriggerManualDispatchInput,
    TriggerManualDispatchUseCase,
)
from mad.core.orchestration.use_cases.update_dispatch_policy import (
    UpdateDispatchPolicyInput,
    UpdateDispatchPolicyUseCase,
)
from mad.core.orchestration.use_cases.update_dispatch_priority import (
    UpdateDispatchPriorityInput,
    UpdateDispatchPriorityUseCase,
)
from mad.core.sessions import SessionStore
from mad.core.sessions.ports.outbound.agent_launcher import AgentLauncher
from mad.core.sessions.ports.outbound.session_repository import SessionRepository
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner
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


def _transport_security() -> TransportSecuritySettings:
    """Build the MCP transport-security policy from the environment.

    MCP's DNS-rebinding protection defaults to ON with an empty host
    allowlist, which rejects *every* Host header — including the
    Cloudflare Tunnel hostname this adapter is designed to be reached
    through. That contradicts Mad's deliberate posture: the security
    boundary is Cloudflare Access at the edge plus the loopback bind,
    and Mad's source tree carries no auth
    (docs/05-operations/runbooks/cloudflare-tunnel.md, ADR-0006, ADR-0010).
    DNS-rebinding protection guards browser-driven
    *local* servers; it is not the control for a token-gated tunnel.

    So protection is OFF by default. Operators who want in-process
    defense-in-depth set ``MAD_MCP_ALLOWED_HOSTS`` to a comma-separated
    host allowlist, which flips protection ON scoped to those hosts.

    The env read is delegated to the central settings module (issue #97).
    Protection flips ON exactly when ``MAD_MCP_ALLOWED_HOSTS`` carries a
    non-blank value (``source == "env"``) — keyed off the source, not the host
    count, so a value that parses to zero hosts keeps the historical
    "protection on, empty allowlist" behaviour rather than silently disabling.
    """
    allowed_hosts = load_settings().mcp_allowed_hosts
    if allowed_hosts.source == "default":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(allowed_hosts.value),
    )


def build_mcp_server(
    *,
    store: SessionStore,
    session_repo: SessionRepository,
    workspace_provisioner: WorkspaceProvisioner,
    launcher_factory: Callable[[str], AgentLauncher],
    event_emitter: EventEmitter,
    task_projection: object,
    deployment_policy: DeploymentDispatchPolicy,
    event_log_query: EventLogQuery,
    clock: Clock,
    model_catalog: ModelCatalog,
    deployment_model_config: DeploymentModelConfig,
    deployment_effort_config: DeploymentEffortConfig,
    workflow_read_model: object,
) -> FastMCP:
    """Build the FastMCP server bound to the supplied in-process dependencies.

    ``streamable_http_path="/"`` so that mounting the returned app at
    ``/mcp`` on the FastAPI app yields the canonical ``/mcp`` endpoint
    (not ``/mcp/mcp``). ``stateless_http=True`` keeps the single-operator
    deployment simple — no server-side MCP session state to persist.
    """

    mcp = FastMCP(
        "Mad",
        instructions=(
            "Infrastructure tools for the Mad agent runner. Tools return raw "
            "session status; classification (failed / needs attention / safe "
            "to delete) is the caller's job — Mad infers nothing."
        ),
        stateless_http=True,
        streamable_http_path="/",
        transport_security=_transport_security(),
    )

    @mcp.tool(
        name="mad_create_session",
        description="Open a work context: provision an isolated workspace and "
        "mount its resources. Returns the session id, status, and workspace path.",
    )
    async def mad_create_session(
        payload: CreateSessionRequest, idempotency_key: str | None = None
    ) -> dict:
        # 422 validation: if a model is specified, verify it exists in the catalog.
        if payload.model is not None:
            await ListProviderModelsUseCase(catalog=model_catalog).validate_model(
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
                    if hasattr(r.checkout, "model_dump")
                    else r.checkout
                ),
                content=r.content,
            )
            for r in payload.resources
        ]
        use_case = CreateSessionUseCase(
            provisioner=workspace_provisioner,
            sessions_index=store.sessions,
            idempotency_index=store.idempotency,
            emitter=event_emitter,
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
            )
        )
        return output.session.response

    @mcp.tool(
        name="mad_send_message",
        description="Launch work in an existing session. Returns immediately "
        "after enqueue — it does NOT wait for the agent to finish.",
    )
    async def mad_send_message(session_id: str, payload: SendMessageRequest) -> dict:
        use_case = SendUserMessageUseCase(
            sessions_index=store.sessions,
            get_launcher=launcher_factory,
            emitter=event_emitter,
            task_queue=task_projection,
            deployment_model_config=deployment_model_config,
            deployment_effort_config=deployment_effort_config,
        )
        use_case.execute(SendUserMessageInput(session_id=session_id, content=payload.content))
        return {"status": "accepted"}

    @mcp.tool(
        name="mad_list_sessions",
        description="List every known session with its raw status "
        "(running / idle / error / created / deleted). Filtering and ordering "
        "mirror GET /v1/sessions.",
    )
    def mad_list_sessions(
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
        order_by: Literal["created_at", "updated_at"] | None = None,
        order: Literal["asc", "desc"] = "asc",
        include_deleted: bool = False,
    ) -> list[SessionSummaryResponse]:
        use_case = ListSessionsUseCase(
            sessions_index=store.sessions,
            repo=session_repo,
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

    @mcp.tool(
        name="mad_get_session",
        description="Detail of one session: status, workspace, and its full "
        "event log. Raises if the session id is unknown.",
    )
    def mad_get_session(session_id: str) -> SessionDetailResponse:
        use_case = GetSessionUseCase(
            repo=session_repo,
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

    @mcp.tool(
        name="mad_delete_session",
        description="Destroy a session's workspace and mark it deleted. "
        "Raises if the session id is unknown.",
    )
    async def mad_delete_session(session_id: str) -> dict:
        use_case = DeleteSessionUseCase(
            provisioner=workspace_provisioner,
            sessions_index=store.sessions,
            emitter=event_emitter,
            task_queue=task_projection,
        )
        output = await use_case.execute(session_id)
        return {"status": output.status, "session_id": output.session_id}

    @mcp.tool(
        name="mad_cleanup_sessions",
        description="Bulk-delete sessions whose updated_at is older than a cutoff. "
        "Set dry_run=true to preview the ids without destroying anything. "
        "Mirrors POST /v1/sessions/cleanup.",
    )
    async def mad_cleanup_sessions(payload: CleanupSessionsRequest) -> CleanupSessionsResponse:
        cutoff = _as_utc(payload.older_than)
        assert cutoff is not None  # mandatory field; Pydantic guards None
        if cutoff > datetime.now(UTC):
            raise ValueError("older_than is not valid")
        use_case = CleanupSessionsUseCase(
            provisioner=workspace_provisioner,
            sessions_index=store.sessions,
            repo=session_repo,
            emitter=event_emitter,
            task_queue=task_projection,
        )
        output = await use_case.execute(
            CleanupSessionsInput(older_than=cutoff, dry_run=payload.dry_run)
        )
        return CleanupSessionsResponse(
            deleted_session_ids=output.deleted_session_ids,
            would_delete=output.would_delete,
            examined=output.examined,
        )

    # -- Orchestration: task queue (issue #28) --------------------------------

    @mcp.tool(
        name="mad_enqueue_task",
        description="Enqueue a task for a session's dispatch queue. Returns the "
        "task id and queued status. Mirrors POST /v1/sessions/{id}/tasks.",
    )
    async def mad_enqueue_task(session_id: str, payload: EnqueueTaskRequest) -> EnqueueTaskResponse:
        use_case = EnqueueTaskUseCase(
            sessions_index=store.sessions,
            emitter=event_emitter,
            model_catalog=model_catalog,
        )
        output = await use_case.execute(
            EnqueueTaskInput(
                session_id=session_id,
                content=payload.content,
                scheduled_for=payload.scheduled_for,
                model=payload.model,
                effort=payload.effort,
                conversation_mode=payload.conversation_mode,
            )
        )
        return EnqueueTaskResponse(
            task_id=output.task_id,
            session_id=output.session_id,
            scheduled_for=output.scheduled_for,
        )

    @mcp.tool(
        name="mad_list_tasks",
        description="List a session's queued tasks and the in-flight task (if any). "
        "Mirrors GET /v1/sessions/{id}/tasks.",
    )
    def mad_list_tasks(session_id: str) -> ListTasksResponse:
        use_case = ListTasksUseCase(sessions_index=store.sessions, task_queue=task_projection)
        output = use_case.execute(session_id)
        ri = _retry_info_response(task_projection.retry_info(session_id))
        return ListTasksResponse(
            queued=[
                TaskResponse(
                    task_id=t.task_id,
                    session_id=t.session_id,
                    content=t.content,
                    scheduled_for=t.scheduled_for,
                    created_at=t.created_at,
                    model=t.model,
                    effort=t.effort,
                    conversation_mode=t.conversation_mode,
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
                    model=output.in_flight.model,
                    effort=output.in_flight.effort,
                    conversation_mode=output.in_flight.conversation_mode,
                    status="retrying" if ri is not None else "dispatched",
                    retry_info=ri,
                )
                if output.in_flight is not None
                else None
            ),
        )

    @mcp.tool(
        name="mad_cancel_task",
        description="Cancel a queued task by id. Raises if the task is unknown or "
        "already dispatched. Mirrors DELETE /v1/sessions/{id}/tasks/{task_id}.",
    )
    async def mad_cancel_task(session_id: str, task_id: UUID) -> CancelTaskResponse:
        use_case = CancelTaskUseCase(
            sessions_index=store.sessions,
            task_queue=task_projection,
            emitter=event_emitter,
        )
        await use_case.execute(CancelTaskInput(session_id=session_id, task_id=task_id))
        return CancelTaskResponse(task_id=task_id)

    # -- Orchestration: dispatch policy (issues #33, #45) ---------------------

    @mcp.tool(
        name="mad_set_session_dispatch_policy",
        description="Pin a per-session dispatch policy (immediate / work_window / "
        "manual), overriding the deployment default. Mirrors "
        "PATCH /v1/sessions/{id}/dispatch_policy.",
    )
    async def mad_set_session_dispatch_policy(
        session_id: str, payload: DispatchPolicyRequest
    ) -> DispatchPolicyResponse:
        policy = policy_from_dict(payload.model_dump())
        use_case = UpdateDispatchPolicyUseCase(sessions_index=store.sessions, emitter=event_emitter)
        output = await use_case.execute(
            UpdateDispatchPolicyInput(session_id=session_id, policy=policy)
        )
        return DispatchPolicyResponse(
            session_id=output.session_id, policy=policy_to_dict(output.policy)
        )

    @mcp.tool(
        name="mad_clear_session_dispatch_policy",
        description="Clear a session's pinned policy so it re-inherits the "
        "deployment default. Idempotent. Mirrors "
        "DELETE /v1/sessions/{id}/dispatch_policy.",
    )
    async def mad_clear_session_dispatch_policy(session_id: str) -> ClearDispatchPolicyResponse:
        use_case = ClearDispatchPolicyUseCase(
            sessions_index=store.sessions,
            deployment=deployment_policy,
            emitter=event_emitter,
        )
        output = await use_case.execute(ClearDispatchPolicyInput(session_id=session_id))
        return ClearDispatchPolicyResponse(
            session_id=output.session_id,
            effective_policy=policy_to_dict(output.effective_policy),
        )

    @mcp.tool(
        name="mad_trigger_dispatch",
        description="Drain a manual-mode session's queue once. Raises if the "
        "effective policy is not manual. Mirrors "
        "POST /v1/sessions/{id}/dispatch_policy/trigger.",
    )
    def mad_trigger_dispatch(session_id: str) -> TriggerManualDispatchResponse:
        use_case = TriggerManualDispatchUseCase(
            sessions_index=store.sessions,
            task_queue=task_projection,
            deployment=deployment_policy,
        )
        output = use_case.execute(TriggerManualDispatchInput(session_id=session_id))
        return TriggerManualDispatchResponse(session_id=output.session_id, drained=output.drained)

    @mcp.tool(
        name="mad_get_deployment_dispatch_policy",
        description="Read the deployment-wide default dispatch policy that "
        "inheriting sessions honour. Mirrors GET /v1/dispatch_policy.",
    )
    def mad_get_deployment_dispatch_policy() -> DeploymentDispatchPolicyResponse:
        use_case = GetDeploymentDispatchPolicyUseCase(deployment=deployment_policy)
        output = use_case.execute()
        return DeploymentDispatchPolicyResponse(policy=policy_to_dict(output.policy))

    @mcp.tool(
        name="mad_set_deployment_dispatch_policy",
        description="Set the deployment-wide default dispatch policy; every "
        "inheriting session honours it live on the next dispatch evaluation. "
        "Mirrors PUT /v1/dispatch_policy.",
    )
    async def mad_set_deployment_dispatch_policy(
        payload: DispatchPolicyRequest,
    ) -> DeploymentDispatchPolicyResponse:
        policy = policy_from_dict(payload.model_dump())
        use_case = SetDeploymentDispatchPolicyUseCase(
            deployment=deployment_policy, emitter=event_emitter
        )
        output = await use_case.execute(SetDeploymentDispatchPolicyInput(policy=policy))
        return DeploymentDispatchPolicyResponse(policy=policy_to_dict(output.policy))

    # -- Orchestration: priority + global queue (issue #46) -------------------

    @mcp.tool(
        name="mad_set_session_priority",
        description="Set a session's cross-session dispatch priority "
        "([1, 10], higher dispatches first; ties break on the head task's "
        "arrival time). Mirrors PATCH /v1/sessions/{id}/priority.",
    )
    async def mad_set_session_priority(
        session_id: str, payload: UpdatePriorityRequest
    ) -> SessionPriorityResponse:
        use_case = UpdateDispatchPriorityUseCase(
            sessions_index=store.sessions, emitter=event_emitter
        )
        output = await use_case.execute(
            UpdateDispatchPriorityInput(session_id=session_id, priority=payload.priority)
        )
        return SessionPriorityResponse(session_id=output.session_id, priority=output.priority)

    @mcp.tool(
        name="mad_get_queue",
        description="Global queue view across all sessions: in_flight / ready / "
        "scheduled, policy-aware and in true dispatch order (ready[0] is what "
        "dispatches next). Mirrors GET /v1/queue.",
    )
    def mad_get_queue() -> GlobalQueueResponse:
        use_case = GetGlobalQueueUseCase(
            sessions_index=store.sessions,
            task_queue=task_projection,
            clock=clock,
            deployment=deployment_policy,
        )
        output = use_case.execute()
        return GlobalQueueResponse(
            in_flight=_queue_task_entry(output.in_flight) if output.in_flight else None,
            ready=[_queue_task_entry(e) for e in output.ready],
            scheduled=[_scheduled_task_entry(e) for e in output.scheduled],
        )

    # -- Orchestration: workflows (issue #90) ---------------------------------

    @mcp.tool(
        name="mad_create_workflow",
        description="Create a workflow: a DAG of steps where each step is a "
        "session + task, held unqueued until all its depends_on predecessors "
        "complete. A step's github mount may inherit a predecessor's repo via "
        "from_step. Returns the workflow id and status. Mirrors POST /v1/workflows.",
    )
    async def mad_create_workflow(payload: CreateWorkflowRequest) -> CreateWorkflowResponse:
        steps = tuple(_to_domain_step(s) for s in payload.steps)
        use_case = CreateWorkflowUseCase(emitter=event_emitter)
        output = await use_case.execute(CreateWorkflowInput(steps=steps))
        return CreateWorkflowResponse(workflow_id=output.workflow_id, status=output.status)

    @mcp.tool(
        name="mad_get_workflow",
        description="Read a workflow's status (pending / running / completed / "
        "failed) and per-step status. Raises if the workflow id is unknown. "
        "Mirrors GET /v1/workflows/{workflow_id}.",
    )
    def mad_get_workflow(workflow_id: str) -> WorkflowStatusResponse:
        use_case = GetWorkflowUseCase(read_model=workflow_read_model)  # type: ignore[arg-type]
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

    # -- Providers: model discovery (issue #55) --------------------------------

    @mcp.tool(
        name="mad_list_provider_models",
        description="List all models available from each registered provider. Each "
        "provider attempts dynamic discovery from its CLI and falls back to a static "
        "list when unavailable. Mirrors GET /v1/providers/models.",
    )
    async def mad_list_provider_models() -> ProviderModelsResponse:
        use_case = ListProviderModelsUseCase(catalog=model_catalog)
        output = await use_case.execute()
        return ProviderModelsResponse(providers=output.catalog)

    # -- Deployment model config (issue #55) -----------------------------------

    @mcp.tool(
        name="mad_get_deployment_model",
        description="Read the deployment-wide default model. Returns ``null`` when "
        "no default has been set (provider uses its own default). "
        "Mirrors GET /v1/model.",
    )
    def mad_get_deployment_model() -> DeploymentModelResponse:
        use_case = GetDeploymentModelUseCase(config=deployment_model_config)
        output: DeploymentModelOutput = use_case.execute()
        return DeploymentModelResponse(model=output.model)

    @mcp.tool(
        name="mad_set_deployment_model",
        description="Set the deployment-wide default model; every session that has "
        "no per-session override honours it live on the next dispatch. "
        "Mirrors PUT /v1/model.",
    )
    async def mad_set_deployment_model(
        payload: SetDeploymentModelRequest,
    ) -> DeploymentModelResponse:
        use_case = SetDeploymentModelUseCase(config=deployment_model_config, emitter=event_emitter)
        output: DeploymentModelOutput = await use_case.execute(
            SetDeploymentModelInput(model=payload.model)
        )
        return DeploymentModelResponse(model=output.model)

    @mcp.tool(
        name="mad_clear_deployment_model",
        description="Clear the deployment-wide model default so sessions use the "
        "provider's own machine-configured default. Idempotent. "
        "Mirrors DELETE /v1/model.",
    )
    async def mad_clear_deployment_model() -> DeploymentModelResponse:
        use_case = ClearDeploymentModelUseCase(
            config=deployment_model_config, emitter=event_emitter
        )
        output: DeploymentModelOutput = await use_case.execute()
        return DeploymentModelResponse(model=output.model)

    # -- Deployment effort config (issue #60) ----------------------------------

    @mcp.tool(
        name="mad_get_deployment_effort",
        description="Read the deployment-wide default reasoning effort. Returns "
        "``null`` when no default has been set (provider uses its own default). "
        "Mirrors GET /v1/effort.",
    )
    def mad_get_deployment_effort() -> DeploymentEffortResponse:
        use_case = GetDeploymentEffortUseCase(config=deployment_effort_config)
        output: DeploymentEffortOutput = use_case.execute()
        return DeploymentEffortResponse(effort=output.effort)

    @mcp.tool(
        name="mad_set_deployment_effort",
        description="Set the deployment-wide default reasoning effort; every session "
        "that has no per-session override honours it live on the next dispatch. The "
        "value is an opaque pass-through string (claude --effort, opencode --variant), "
        "not validated by Mad. Mirrors PUT /v1/effort.",
    )
    async def mad_set_deployment_effort(
        payload: SetDeploymentEffortRequest,
    ) -> DeploymentEffortResponse:
        use_case = SetDeploymentEffortUseCase(
            config=deployment_effort_config, emitter=event_emitter
        )
        output: DeploymentEffortOutput = await use_case.execute(
            SetDeploymentEffortInput(effort=payload.effort)
        )
        return DeploymentEffortResponse(effort=output.effort)

    @mcp.tool(
        name="mad_clear_deployment_effort",
        description="Clear the deployment-wide reasoning-effort default so sessions "
        "use the provider's own machine-configured default. Idempotent. "
        "Mirrors DELETE /v1/effort.",
    )
    async def mad_clear_deployment_effort() -> DeploymentEffortResponse:
        use_case = ClearDeploymentEffortUseCase(
            config=deployment_effort_config, emitter=event_emitter
        )
        output: DeploymentEffortOutput = await use_case.execute()
        return DeploymentEffortResponse(effort=output.effort)

    # -- Config: effective operational configuration (issue #107) -------------

    @mcp.tool(
        name="mad_get_config",
        description="Read the server's effective operational configuration: each "
        "MAD_* tunable as {value, source: env|default}, plus credential presence "
        "booleans (never the values). Read-only. Mirrors GET /v1/config.",
    )
    def mad_get_config() -> ConfigResponse:
        settings = GetConfigUseCase().execute()
        return build_config_response(settings)

    # -- Events: historical query (issue #32) ---------------------------------

    @mcp.tool(
        name="mad_query_events",
        description="Paginated historical event query across sessions. Filter by "
        "session_id / kind / agent / since. Mirrors GET /v1/events (the streaming "
        "SSE surface is intentionally not a tool).",
    )
    def mad_query_events(
        session_id: str | None = None,
        kind: str | None = None,
        agent: str | None = None,
        since: datetime | None = None,
        after_event_id: UUID | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        use_case = QueryEventsUseCase(log=event_log_query)
        output = use_case.execute(
            QueryEventsInput(
                session_id=session_id,
                kind=kind,
                agent=agent,
                since=since,
                after_event_id=after_event_id,
                limit=limit,
            )
        )
        return {
            "events": [_serialize_event(e) for e in output.events],
            "next_cursor": str(output.next_cursor) if output.next_cursor is not None else None,
        }

    return mcp
