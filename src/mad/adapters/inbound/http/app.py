from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mad.adapters.inbound.http.dependencies import build_dependencies, touch_session
from mad.adapters.inbound.http.routes.events import router as events_router
from mad.adapters.inbound.http.routes.orchestration import router as orchestration_router
from mad.adapters.inbound.http.routes.sessions import router as sessions_router
from mad.adapters.outbound.agents import factory
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.adapters.outbound.persistence.jsonl_session_repository import ensure_sessions_dir
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.orchestration.domain.exceptions.base import (
    SessionHasInFlightTask,
    TaskAlreadyDispatched,
    TaskNotFound,
)
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
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
    task_projection: InMemoryTaskProjection | None = None,
    dispatcher: Dispatcher | None = None,
) -> FastAPI:
    """Build a FastAPI app with injected dependencies."""

    (
        _default_store,
        _default_repo,
        _default_provisioner,
        _default_event_bus,
        _default_event_log_query,
        _default_event_emitter,
        _default_projection,
    ) = build_dependencies()

    final_store = store if store is not None else _default_store
    final_repo = session_repo if session_repo is not None else _default_repo
    final_provisioner = (
        workspace_provisioner if workspace_provisioner is not None else _default_provisioner
    )
    final_launcher_factory: Callable[[str], AgentLauncher] = (
        launcher_factory if launcher_factory is not None else factory.get_launcher
    )
    final_event_bus = event_bus if event_bus is not None else _default_event_bus
    final_event_log_query = (
        event_log_query if event_log_query is not None else _default_event_log_query
    )
    final_projection = task_projection if task_projection is not None else _default_projection

    if event_emitter is not None:
        final_event_emitter = event_emitter
    elif store is not None:
        # User supplied a custom store: rebind the default emitter's hook so
        # ``Session.updated_at`` mutations land on THEIR store, not the
        # discarded default one.
        _default_event_emitter._on_emit = touch_session(final_store)
        final_event_emitter = _default_event_emitter
    else:
        final_event_emitter = _default_event_emitter

    final_dispatcher = (
        dispatcher
        if dispatcher is not None
        else Dispatcher(
            projection=final_projection,
            emitter=final_event_emitter,
            bus=final_event_bus,
            sessions_index=final_store.sessions,
            get_launcher=final_launcher_factory,
        )
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ensure_sessions_dir()
        # Bootstrap the orchestration projection from the persisted log
        # before the dispatcher's orphan recovery runs (ADR-0009 Decision 5).
        final_projection.bootstrap_from_log(final_event_log_query)
        await final_dispatcher.start()
        try:
            yield
        finally:
            await final_dispatcher.stop()

    app = FastAPI(title="Mad", version="0.3.0", lifespan=lifespan)
    app.state.store = final_store
    app.state.session_repo = final_repo
    app.state.workspace_provisioner = final_provisioner
    app.state.launcher_factory = final_launcher_factory
    app.state.event_bus = final_event_bus
    app.state.event_log_query = final_event_log_query
    app.state.event_emitter = final_event_emitter
    app.state.task_projection = final_projection
    app.state.dispatcher = final_dispatcher

    @app.exception_handler(PathTraversalError)
    async def _path_traversal_handler(request: Request, exc: PathTraversalError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SessionNotFound)
    async def _session_not_found_handler(request: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(TaskNotFound)
    async def _task_not_found_handler(request: Request, exc: TaskNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(TaskAlreadyDispatched)
    async def _task_already_dispatched_handler(
        request: Request, exc: TaskAlreadyDispatched
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(SessionHasInFlightTask)
    async def _session_has_in_flight_task_handler(
        request: Request, exc: SessionHasInFlightTask
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.include_router(sessions_router)
    app.include_router(events_router)
    app.include_router(orchestration_router)
    return app
