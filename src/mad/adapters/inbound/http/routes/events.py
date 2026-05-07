"""Events HTTP routes — cross-session observability surface (issue #10).

Two endpoints, both filterable by ``session_id``, ``kind``, and
``agent``:

- ``GET /v1/events`` — paginated historical query. Returns
  ``{events: [...], next_cursor: <event_id | null>}``. The cursor is
  the ``event_id`` of the last returned event when more results are
  available; clients pass it as ``after_event_id`` to fetch the next
  page.

- ``GET /v1/events/stream`` — long-lived SSE connection that emits
  every matching event as it occurs. Honours the standard
  ``Last-Event-ID`` request header: when present, the endpoint first
  replays events from the JSONL log, then transitions to the live
  tail with no gap and no duplicates (ADR-0004 dedup boundary).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from mad.core.events.domain.event import Event
from mad.core.events.ports.event_bus import EventBus
from mad.core.events.ports.event_log_query import EventLogQuery
from mad.core.events.use_cases.query_events import (
    QueryEventsInput,
    QueryEventsUseCase,
)
from mad.core.events.use_cases.stream_events import (
    StreamEventsInput,
    StreamEventsUseCase,
)

router = APIRouter(tags=["events"])


def _parse_last_event_id(header: str | None) -> UUID | None:
    """Tolerant ``Last-Event-ID`` parser.

    Returns the parsed UUID, or ``None`` for missing / empty / malformed
    values. SSE clients (browsers, Postman) sometimes attach this header
    automatically on first connect with an empty or stale value; refusing
    the connection in that case would make the endpoint unusable from
    those clients without manual header surgery.
    """
    if not header:
        return None
    try:
        return UUID(header)
    except ValueError:
        return None


def _bus(request: Request) -> EventBus:
    return request.app.state.event_bus


def _log(request: Request) -> EventLogQuery:
    return request.app.state.event_log_query


def _serialize_event(event: Event) -> dict[str, Any]:
    return {
        "event_id": str(event.event_id) if event.event_id is not None else None,
        "session_id": event.session_id,
        "type": event.type,
        "data": event.data,
        "timestamp": event.timestamp.isoformat(),
    }


@router.get("/v1/events")
async def list_events(
    request: Request,
    session_id: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    since: Annotated[datetime | None, Query()] = None,
    after_event_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    use_case = QueryEventsUseCase(log=_log(request))
    output = use_case.execute(
        QueryEventsInput(
            session_id=session_id,
            kind=kind,
            agent=agent,
            since=since,
            after_event_id=after_event_id,
            limit=limit,
        )
    )
    return {
        "events": [_serialize_event(e) for e in output.events],
        "next_cursor": str(output.next_cursor) if output.next_cursor is not None else None,
    }


@router.get("/v1/events/stream")
async def stream_events(
    request: Request,
    session_id: Annotated[str | None, Query()] = None,
    kind: Annotated[str | None, Query()] = None,
    agent: Annotated[str | None, Query()] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    parsed_last = _parse_last_event_id(last_event_id_header)
    use_case = StreamEventsUseCase(bus=_bus(request), log=_log(request))

    payload = StreamEventsInput(
        session_id=session_id,
        kind=kind,
        agent=agent,
        last_event_id=parsed_last,
    )

    async def event_generator():
        async for event in use_case.execute(payload):
            serialized = _serialize_event(event)
            id_line = (
                f"id: {serialized['event_id']}\n" if serialized["event_id"] is not None else ""
            )
            yield f"{id_line}data: {json.dumps(serialized)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
