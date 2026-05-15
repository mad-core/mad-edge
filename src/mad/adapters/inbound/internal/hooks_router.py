"""Internal hook ingestion router.

Receives hook payloads from forward.sh running inside each workspace
over a Unix Domain Socket. This router is NEVER mounted on the public app.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter()

_CREDENTIAL_KEYS = {"token", "authorization", "api_key", "password", "secret"}
_ANT_TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def _scrub_credentials(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a new dict with credential-shaped strings replaced by [REDACTED].

    Walks recursively. Does not mutate the input.
    """
    if data is None:
        return None

    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in _CREDENTIAL_KEYS:
            result[key] = "[REDACTED]"
        elif isinstance(value, str):
            result[key] = _ANT_TOKEN_RE.sub("[REDACTED]", value)
        elif isinstance(value, dict):
            result[key] = _scrub_credentials(value)
        else:
            result[key] = value
    return result


class HookIngestRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    type: str = Field(..., pattern=r"^agent\.[a-z_]+\.hook\.[A-Za-z]+$")
    data: dict[str, Any] | None = None


class HookIngestResponse(BaseModel):
    event_id: str


@router.post(
    "/_internal/hooks",
    response_model=HookIngestResponse,
    status_code=202,
    include_in_schema=False,
)
async def ingest_hook(payload: HookIngestRequest, request: Request) -> HookIngestResponse:
    emitter = request.app.state.event_emitter
    scrubbed_data = _scrub_credentials(payload.data)
    event = await emitter.emit(payload.session_id, payload.type, scrubbed_data)
    return HookIngestResponse(event_id=str(event.event_id))
