"""Session endpoints — thin HTTP layer.

Each handler:
  1. Parses the HTTP request (JSON + headers).
  2. Instantiates the relevant use case with dependencies from app.state.
  3. Calls use_case.execute(input).
  4. Maps the result (or domain exception) to an HTTP response.

Business logic lives in mad.core.sessions.use_cases.*.
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Request

from mad.core.sessions import SessionStore
from mad.core.sessions.use_cases.create_session import (
    CreateSessionInput,
    CreateSessionUseCase,
    ResourceSpec,
)
from mad.core.sessions.use_cases.delete_session import DeleteSessionUseCase
from mad.core.sessions.use_cases.get_session import GetSessionUseCase
from mad.core.sessions.use_cases.list_sessions import ListSessionsUseCase
from mad.core.sessions.use_cases.send_user_message import (
    SendUserMessageInput,
    SendUserMessageUseCase,
)

router = APIRouter()


def _store(request: Request) -> SessionStore:
    return request.app.state.store


def _repo(request: Request):
    return request.app.state.session_repo


def _provisioner(request: Request):
    return request.app.state.workspace_provisioner


@router.post("/v1/sessions")
async def create_session(
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict:
    store = _store(request)
    body = await request.json()
    agent = body["agent"]
    raw_resources = body.get("resources", [])
    base_branch = body.get("base_branch")

    resource_specs = [
        ResourceSpec(
            type=r["type"],
            mount_path=r["mount_path"],
            url=r.get("url", ""),
            authorization_token=r.get("authorization_token"),
            checkout=r.get("checkout"),
            content=r.get("content", ""),
        )
        for r in raw_resources
    ]

    use_case = CreateSessionUseCase(
        provisioner=_provisioner(request),
        sessions_index=store.sessions,
        idempotency_index=store.idempotency,
        emitter=request.app.state.event_emitter,
    )

    output = await use_case.execute(
        CreateSessionInput(
            agent=agent,
            resources=resource_specs,
            idempotency_key=idempotency_key,
            base_branch=base_branch,
        )
    )

    return output.session.response


@router.post("/v1/sessions/{session_id}/events")
async def send_events(session_id: str, request: Request) -> dict:
    store = _store(request)
    body = await request.json()
    events = body.get("events", [])

    use_case = SendUserMessageUseCase(
        sessions_index=store.sessions,
        get_launcher=request.app.state.launcher_factory,
        emitter=request.app.state.event_emitter,
    )

    for event in events:
        if event.get("type") == "user.message":
            content = event.get("content", "")
            use_case.execute(SendUserMessageInput(session_id=session_id, content=content))

    return {"status": "accepted"}


@router.get("/v1/sessions/{session_id}")
async def get_session(session_id: str, request: Request) -> dict:
    store = _store(request)

    use_case = GetSessionUseCase(
        repo=_repo(request),
        sessions_index=store.sessions,
    )
    output = use_case.execute(session_id)

    return {
        "session_id": output.session_id,
        "status": output.status,
        "workspace": output.workspace,
        "events": output.events,
    }


@router.get("/v1/sessions")
async def list_sessions(request: Request) -> list:
    store = _store(request)

    use_case = ListSessionsUseCase(sessions_index=store.sessions)
    summaries = use_case.execute()
    return [{"session_id": s.session_id, "status": s.status} for s in summaries]


@router.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> dict:
    store = _store(request)

    use_case = DeleteSessionUseCase(
        provisioner=_provisioner(request),
        sessions_index=store.sessions,
        emitter=request.app.state.event_emitter,
    )
    output = await use_case.execute(session_id)
    return {"status": output.status, "session_id": output.session_id}
