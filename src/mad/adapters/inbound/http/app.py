from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mad.adapters.inbound.http.dependencies import build_dependencies
from mad.adapters.inbound.http.routes.events import router as events_router
from mad.adapters.inbound.http.routes.sessions import router as sessions_router
from mad.adapters.outbound.agents import factory
from mad.adapters.outbound.persistence.jsonl_session_repository import ensure_sessions_dir
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.sessions import SessionStore
from mad.core.sessions.domain.exceptions.base import PathTraversalError, SessionNotFound
from mad.core.sessions.ports.outbound.agent_launcher import AgentLauncher
from mad.core.sessions.ports.outbound.session_repository import SessionRepository
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner


def create_app(
    store: SessionStore | None = None,
    session_repo: SessionRepository | None = None,
    workspace_provisioner: WorkspaceProvisioner | None = None,
    launcher_factory: Callable[[str], AgentLauncher] | None = None,
    event_bus: EventBus | None = None,
    event_log_query: EventLogQuery | None = None,
    event_emitter: EventEmitter | None = None,
) -> FastAPI:
    """Build a FastAPI app with injected dependencies.

    Every call creates an isolated instance — tests get a fresh store,
    bus, and log query so state never leaks across cases.

    If a port is not supplied, ``build_dependencies`` provides the
    production default:
    - ``JsonlSessionRepository`` — JSONL file-backed session log.
    - ``LocalWorkspaceProvisioner`` — local temp-dir workspace management.
    - ``InMemoryEventBus`` — asyncio fanout for live event subscribers.
    - ``JsonlEventLogQuery`` — read-side query over the same JSONL log.
    - ``EventEmitter`` — single write gateway (EventStore + EventBus).

    Routes delegate to use cases from ``mad.core.sessions.use_cases.*``
    and ``mad.core.events.use_cases.*``.
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ensure_sessions_dir()
        yield

    app = FastAPI(title="Mad", version="0.3.0", lifespan=lifespan)
    (
        _default_store,
        _default_repo,
        _default_provisioner,
        _default_event_bus,
        _default_event_log_query,
        _default_event_emitter,
    ) = build_dependencies()
    app.state.store = store if store is not None else _default_store
    app.state.session_repo = session_repo if session_repo is not None else _default_repo
    app.state.workspace_provisioner = (
        workspace_provisioner if workspace_provisioner is not None else _default_provisioner
    )
    app.state.launcher_factory = (
        launcher_factory if launcher_factory is not None else factory.get_launcher
    )
    app.state.event_bus = event_bus if event_bus is not None else _default_event_bus
    app.state.event_log_query = (
        event_log_query if event_log_query is not None else _default_event_log_query
    )
    app.state.event_emitter = event_emitter if event_emitter is not None else _default_event_emitter

    @app.exception_handler(PathTraversalError)
    async def _path_traversal_handler(request: Request, exc: PathTraversalError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SessionNotFound)
    async def _session_not_found_handler(request: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    app.include_router(sessions_router)
    app.include_router(events_router)
    return app
