"""FastMCP server exposing Mad's session use cases as MCP tools.

Five tools, ~1:1 with the existing HTTP routes. Each tool instantiates
the same use case the HTTP handler uses, against the same in-process
dependencies (store / repo / provisioner / emitter / launcher factory),
and returns the same Pydantic shapes the HTTP layer returns — so the
MCP boundary cannot drift from the REST boundary by construction
(CLAUDE.md hard rule 9, ADR-0010).

What this module deliberately does NOT expose: `agent.*` output, hook
events, or the cross-session event stream. Those are operator telemetry
on the existing SSE surface, not orchestrator tools (issue #32, ADR-0004).
The "which failed / needs attention / can I delete" reasoning belongs to
the orchestrator LLM reading `mad_list_sessions`; Mad returns raw
``status`` only and infers nothing (hard rule 1).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from mad.adapters.inbound.http.routes.sessions import (
    CreateSessionRequest,
    SendMessageRequest,
    SessionDetailResponse,
    SessionSummaryResponse,
    _as_utc,
)
from mad.core.events.emitter import EventEmitter
from mad.core.sessions import SessionStore
from mad.core.sessions.ports.outbound.agent_launcher import AgentLauncher
from mad.core.sessions.ports.outbound.session_repository import SessionRepository
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner
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
    and Mad's source tree carries no auth (docs/cloudflare-tunnel.md,
    ADR-0006, ADR-0010). DNS-rebinding protection guards browser-driven
    *local* servers; it is not the control for a token-gated tunnel.

    So protection is OFF by default. Operators who want in-process
    defense-in-depth set ``MAD_MCP_ALLOWED_HOSTS`` to a comma-separated
    host allowlist, which flips protection ON scoped to those hosts.
    """
    raw = os.environ.get("MAD_MCP_ALLOWED_HOSTS", "").strip()
    if not raw:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    hosts = [h.strip() for h in raw.split(",") if h.strip()]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
    )


def build_mcp_server(
    *,
    store: SessionStore,
    session_repo: SessionRepository,
    workspace_provisioner: WorkspaceProvisioner,
    launcher_factory: Callable[[str], AgentLauncher],
    event_emitter: EventEmitter,
    task_projection: object,
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
        )
        output = await use_case.execute(session_id)
        return {"status": output.status, "session_id": output.session_id}

    return mcp
