"""Composition root — builds default infrastructure dependencies.

``build_dependencies`` is called by ``create_app`` when callers do not
supply explicit overrides (the common production path). Tests pass
their own fakes via the ``create_app`` keyword arguments and never go
through this function.
"""

from __future__ import annotations

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.events.jsonl_event_log_query import JsonlEventLogQuery
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
)
from mad.adapters.outbound.persistence.local_workspace_provisioner import (
    LocalWorkspaceProvisioner,
)
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
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
]:
    """Return the production defaults for every injected port."""
    repo = JsonlSessionRepository()
    bus = InMemoryEventBus()
    emitter = EventEmitter(store=repo, bus=bus)
    return (
        SessionStore(),
        repo,
        LocalWorkspaceProvisioner(),
        bus,
        JsonlEventLogQuery(),
        emitter,
    )
