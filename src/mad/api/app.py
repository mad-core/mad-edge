from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

from mad.api.routes.sessions import router as sessions_router
from mad.core import log
from mad.core.exceptions import PathTraversalError
from mad.core.sessions import SessionStore


def create_app(store: SessionStore | None = None) -> FastAPI:
    """Build a FastAPI app with an injected SessionStore.

    Every call creates an isolated instance — tests get a fresh store
    so state never leaks across cases.
    """
    app = FastAPI(title="Mad", version="0.1.0")
    app.state.store = store or SessionStore()

    @app.exception_handler(PathTraversalError)
    async def _path_traversal_handler(request: Request, exc: PathTraversalError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.on_event("startup")
    async def _startup() -> None:
        log.ensure_sessions_dir()

    app.include_router(sessions_router)
    return app
