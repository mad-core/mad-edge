from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mad.adapters.inbound.http.dependencies import build_dependencies, touch_session
from mad.adapters.inbound.http.routes.events import router as events_router
from mad.adapters.inbound.http.routes.orchestration import router as orchestration_router
from mad.adapters.inbound.http.routes.providers import router as providers_router
from mad.adapters.inbound.http.routes.sessions import router as sessions_router
from mad.adapters.outbound.agents import factory
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    ensure_sessions_dir,
    purge_expired_logs,
    resolve_retention_days,
)
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.orchestration.domain.deployment_policy import DeploymentDispatchPolicy
from mad.core.orchestration.domain.dispatch_policy import InvalidDispatchPolicy
from mad.core.orchestration.domain.effort_config import DeploymentEffortConfig
from mad.core.orchestration.domain.exceptions.base import (
    SessionHasInFlightTask,
    TaskAlreadyDispatched,
    TaskNotFound,
)
from mad.core.orchestration.domain.model_config import DeploymentModelConfig
from mad.core.orchestration.domain.ordering import InvalidPriority
from mad.core.orchestration.ports.clock import Clock
from mad.core.orchestration.ports.model_catalog import ModelCatalog
from mad.core.orchestration.use_cases.deployment_dispatch_policy import (
    bootstrap_deployment_policy,
)
from mad.core.orchestration.use_cases.deployment_effort_config import (
    bootstrap_deployment_effort_config,
)
from mad.core.orchestration.use_cases.deployment_model_config import (
    bootstrap_deployment_model_config,
)
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.list_provider_models import InvalidModelError
from mad.core.orchestration.use_cases.rehydrate_pending_sessions import (
    rehydrate_pending_sessions,
)
from mad.core.orchestration.use_cases.trigger_manual_dispatch import TriggerNotApplicable
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
    clock: Clock | None = None,
    dispatcher_tick_interval_s: float | None = None,
    deployment_policy: DeploymentDispatchPolicy | None = None,
    model_catalog: ModelCatalog | None = None,
    deployment_model_config: DeploymentModelConfig | None = None,
    deployment_effort_config: DeploymentEffortConfig | None = None,
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
        _default_clock,
        _default_deployment_policy,
        _default_model_catalog,
        _default_deployment_model_config,
        _default_deployment_effort_config,
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
    final_clock: Clock = clock if clock is not None else _default_clock
    final_deployment_policy = (
        deployment_policy if deployment_policy is not None else _default_deployment_policy
    )

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

    final_model_catalog: ModelCatalog = (
        model_catalog if model_catalog is not None else _default_model_catalog
    )
    final_deployment_model_config: DeploymentModelConfig = (
        deployment_model_config
        if deployment_model_config is not None
        else _default_deployment_model_config
    )
    final_deployment_effort_config: DeploymentEffortConfig = (
        deployment_effort_config
        if deployment_effort_config is not None
        else _default_deployment_effort_config
    )

    if dispatcher is not None:
        final_dispatcher = dispatcher
    else:
        dispatcher_kwargs: dict[str, object] = {
            "projection": final_projection,
            "emitter": final_event_emitter,
            "bus": final_event_bus,
            "sessions_index": final_store.sessions,
            "get_launcher": final_launcher_factory,
            "clock": final_clock,
            "deployment_policy": final_deployment_policy,
            "deployment_model_config": final_deployment_model_config,
            "deployment_effort_config": final_deployment_effort_config,
        }
        if dispatcher_tick_interval_s is not None:
            dispatcher_kwargs["tick_interval_s"] = dispatcher_tick_interval_s
        final_dispatcher = Dispatcher(**dispatcher_kwargs)  # type: ignore[arg-type]

    # MCP inbound adapter (ADR-0010): same process, same dependencies as the
    # HTTP routes — tools call use cases in-process, not Mad's own HTTP. The
    # session manager must run inside an async context, so it is entered in
    # the app lifespan alongside the dispatcher. Imported lazily: the MCP
    # adapter reuses the HTTP route models, so a module-level import here
    # would form an import cycle through this package's __init__.
    from mad.adapters.inbound.mcp import build_mcp_server

    mcp_server = build_mcp_server(
        store=final_store,
        session_repo=final_repo,
        workspace_provisioner=final_provisioner,
        launcher_factory=final_launcher_factory,
        event_emitter=final_event_emitter,
        task_projection=final_projection,
        deployment_policy=final_deployment_policy,
        event_log_query=final_event_log_query,
        clock=final_clock,
        model_catalog=final_model_catalog,
        deployment_model_config=final_deployment_model_config,
        deployment_effort_config=final_deployment_effort_config,
    )
    mcp_asgi_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        ensure_sessions_dir()
        # Enforce the optional JSONL log retention TTL at startup (issue #14).
        # Unset / non-positive MAD_SESSIONS_RETENTION_DAYS -> None -> disabled,
        # which preserves the historical never-purge behavior (safe default).
        retention_days = resolve_retention_days()
        if retention_days is not None:
            purge_expired_logs(datetime.now(UTC), retention_days)
        # Bootstrap the orchestration projection from the persisted log
        # before the dispatcher's orphan recovery runs (ADR-0009 Decision 5).
        final_projection.bootstrap_from_log(final_event_log_query)
        # Rebuild every session with pending work into the live index
        # BEFORE the dispatcher starts (issue #46 Part A) — otherwise
        # queued work never resumes after a restart until each owning
        # session is individually fetched.
        rehydrate_pending_sessions(final_projection, final_repo, final_store.sessions)
        # Rebuild the deployment-wide default policy from its reserved log
        # so an operator-set default survives a restart (issue #45,
        # hard rule 6) before the dispatcher evaluates any session.
        bootstrap_deployment_policy(final_deployment_policy, final_repo)
        # Rebuild the deployment-wide model default from its reserved log
        # (issue #55, hard rule 6).
        bootstrap_deployment_model_config(final_deployment_model_config, final_repo)
        # Rebuild the deployment-wide effort default from its reserved log
        # (issue #60, hard rule 6).
        bootstrap_deployment_effort_config(final_deployment_effort_config, final_repo)
        await final_dispatcher.start()
        async with mcp_server.session_manager.run():
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
    app.state.clock = final_clock
    app.state.deployment_policy = final_deployment_policy
    app.state.model_catalog = final_model_catalog
    app.state.deployment_model_config = final_deployment_model_config
    app.state.deployment_effort_config = final_deployment_effort_config
    app.state.mcp_server = mcp_server

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

    @app.exception_handler(InvalidDispatchPolicy)
    async def _invalid_dispatch_policy_handler(
        request: Request, exc: InvalidDispatchPolicy
    ) -> JSONResponse:
        # InvalidDispatchPolicy inherits ValueError but the dispatcher
        # contract treats it as a bad request body — 422 makes the bug
        # locatable in the caller's payload, not 400 (which the generic
        # ValueError handler would otherwise emit).
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(InvalidPriority)
    async def _invalid_priority_handler(request: Request, exc: InvalidPriority) -> JSONResponse:
        # Same reasoning as InvalidDispatchPolicy: an out-of-range
        # priority is a defect in the caller's payload — 422, never a
        # silent clamp (issue #46 hard rule).
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(InvalidModelError)
    async def _invalid_model_handler(request: Request, exc: InvalidModelError) -> JSONResponse:
        # InvalidModelError inherits ValueError; 422 identifies the unknown
        # model as a validation error on the caller's payload.
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(TriggerNotApplicable)
    async def _trigger_not_applicable_handler(
        request: Request, exc: TriggerNotApplicable
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    app.include_router(sessions_router)
    app.include_router(events_router)
    app.include_router(orchestration_router)
    app.include_router(providers_router)
    app.mount("/mcp", mcp_asgi_app)
    return app
