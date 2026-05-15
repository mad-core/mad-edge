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

The stream emits a transport-level keepalive comment frame
(``: ping\\n\\n``) whenever no domain event has been published within
``MAD_SSE_HEARTBEAT_S`` seconds (default 15). The frame carries no
``id:`` line and is parsed-and-ignored by every conformant SSE client,
so it never advances ``Last-Event-ID`` catch-up and is never written
to the JSONL log (issue #34). The response also sets
``Cache-Control: no-cache, no-transform`` and ``X-Accel-Buffering: no``
so buffering reverse proxies (Cloudflare Tunnel, nginx default, …)
flush frames as they arrive instead of stalling the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
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


_HEARTBEAT_DEFAULT_S = 15.0
_HEARTBEAT_FRAME = ": ping\n\n"
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def _heartbeat_interval() -> float:
    """Resolve ``MAD_SSE_HEARTBEAT_S`` with a safe default.

    Missing, unparseable, or non-positive values fall back to the
    default so a misconfiguration cannot silently disable the
    heartbeat behind a buffering proxy.
    """
    raw = os.environ.get("MAD_SSE_HEARTBEAT_S")
    if raw is None:
        return _HEARTBEAT_DEFAULT_S
    try:
        value = float(raw)
    except ValueError:
        return _HEARTBEAT_DEFAULT_S
    if value <= 0:
        return _HEARTBEAT_DEFAULT_S
    return value


async def _fetch_next_frame(aiter: AsyncIterator[str]) -> str:
    """Wrap ``aiter.__anext__()`` so ``asyncio.create_task`` accepts it.

    ``__anext__`` is typed as ``Awaitable[str]`` whereas ``create_task``
    requires a coroutine — this thin wrapper bridges the contracts
    without changing semantics (StopAsyncIteration still propagates).
    """
    return await aiter.__anext__()


async def _with_heartbeat(source: AsyncIterator[str], interval: float) -> AsyncIterator[str]:
    """Yield from ``source``; inject ``: ping\\n\\n`` when idle.

    Races each ``__anext__`` against ``interval``-second waits. On
    timeout, yields a comment-frame heartbeat WITHOUT cancelling the
    pending fetch, so a buffered event is never dropped across
    heartbeats. On client disconnect the surrounding ``StreamingResponse``
    cancels this coroutine, the ``finally`` block cancels the pending
    fetch, and the bus subscription disposes via its own ``aclose``.
    """
    aiter = source.__aiter__()
    pending: asyncio.Task[str] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(_fetch_next_frame(aiter))
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if pending in done:
                try:
                    frame = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield frame
            else:
                yield _HEARTBEAT_FRAME
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending


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

    async def event_generator() -> AsyncIterator[str]:
        async for event in use_case.execute(payload):
            serialized = _serialize_event(event)
            id_line = (
                f"id: {serialized['event_id']}\n" if serialized["event_id"] is not None else ""
            )
            yield f"{id_line}data: {json.dumps(serialized)}\n\n"

    return StreamingResponse(
        _with_heartbeat(event_generator(), _heartbeat_interval()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
