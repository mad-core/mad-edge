"""Internal FastAPI app for hook ingestion.

Separate from the public app — never exposes docs, redoc, or openapi.
Receives payloads from forward.sh over a Unix Domain Socket.
"""

from __future__ import annotations

from fastapi import FastAPI

from mad.core.events.emitter import EventEmitter

from .hooks_router import router as hooks_router


def create_internal_app(event_emitter: EventEmitter) -> FastAPI:
    """Build the internal hook ingestion app.

    Args:
        event_emitter: The single write gateway; stored on app.state so the
                       router can retrieve it per-request.

    Returns:
        A FastAPI instance with no public docs surfaces and a single
        ``POST /_internal/hooks`` route.
    """
    app = FastAPI(
        title="Mad Internal",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.event_emitter = event_emitter
    app.include_router(hooks_router)
    return app
