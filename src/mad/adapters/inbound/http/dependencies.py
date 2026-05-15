"""Composition root — builds default infrastructure dependencies.

``build_dependencies`` is called by ``create_app`` when callers do not
supply explicit overrides (the common production path). Tests pass
their own fakes via the ``create_app`` keyword arguments and never go
through this function.
"""

from __future__ import annotations

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.events.jsonl_event_log_query import JsonlEventLogQuery
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.adapters.outbound.orchestration.system_clock import SystemClock
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
)
from mad.adapters.outbound.persistence.local_workspace_provisioner import (
    LocalWorkspaceProvisioner,
)
from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.orchestration.ports.clock import Clock
from mad.core.sessions import SessionStore
from mad.core.sessions.ports.outbound.session_repository import SessionRepository
from mad.core.sessions.ports.outbound.workspace_provisioner import WorkspaceProvisioner


def build_dependencies() -> tuple[
    SessionStore,
    SessionRepository,
    WorkspaceProvisioner,
    EventBus,
    EventLogQuery,
    EventEmitter,
    InMemoryTaskProjection,
    Clock,
]:
    """Return the production defaults for every injected port."""
    store = SessionStore()
    repo = JsonlSessionRepository()
    bus = InMemoryEventBus()
    emitter = EventEmitter(store=repo, bus=bus, on_emit=touch_session(store))
    projection = InMemoryTaskProjection()
    clock: Clock = SystemClock()
    return (
        store,
        repo,
        LocalWorkspaceProvisioner(),
        bus,
        JsonlEventLogQuery(),
        emitter,
        projection,
        clock,
    )


def touch_session(store: SessionStore):
    """Return an ``on_emit`` hook that bumps ``Session.updated_at`` for the
    in-memory entity matching ``event.session_id`` (if any).

    Sessions only present on disk (rehydrated lazily by use cases) derive
    their ``updated_at`` from the persisted event stream, not from this
    hook — so missing entries here are not a bug.
    """

    def _on_emit(event: Event) -> None:
        session = store.sessions.get(event.session_id)
        if session is not None:
            session.touch(event.timestamp)

    return _on_emit
