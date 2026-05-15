"""HTTP integration tests for the events module endpoints.

Exercises ``GET /v1/events`` and ``GET /v1/events/stream`` against a
fully-wired ``create_app`` with the JSONL persistence stack pointed at
``tmp_sessions_dir`` (CLAUDE.md hard rule 6 — log is the source of
truth).

Live-tail SSE behavior is covered at the use-case unit-test level
(``tests/unit/core/events/use_cases/test_stream_events.py``); here we
verify HTTP framing, the ``Last-Event-ID`` replay path, filter
pass-through, and the transport-level heartbeat / anti-buffering
behavior introduced in issue #34.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
)
from mad.core.events.domain.event import Event
from mad.core.events.domain.event_id import new_event_id
from support.events import FakeEventBus, FakeEventLogQuery


@pytest.fixture
def http_client(tmp_sessions_dir: Path, tmp_workspaces_dir: Path) -> TestClient:
    return TestClient(create_app())


def _write_session_events(
    repo: JsonlSessionRepository, session_id: str, types: list[tuple[str, dict]]
) -> list[dict]:
    """Append events to one session's log, forcing a 2 ms gap between
    writes so UUIDv7 timestamps advance and sort order is deterministic."""
    written = []
    for i, (event_type, data) in enumerate(types):
        if i > 0:
            time.sleep(0.002)
        written.append(repo.append_event(session_id, event_type, data))
    return written


# ---- GET /v1/events ---------------------------------------------------------


def test_get_events_returns_persisted_events(http_client: TestClient) -> None:
    repo = JsonlSessionRepository()
    _write_session_events(
        repo,
        "sesn_a",
        [
            ("session.created", {"agent": "claude_cli"}),
            ("agent.output", {"line": "hi"}),
            ("session.status_idle", {"stop_reason": "end_turn"}),
        ],
    )

    response = http_client.get("/v1/events")

    assert response.status_code == 200
    body = response.json()
    types = [e["type"] for e in body["events"]]
    assert types == ["session.created", "agent.output", "session.status_idle"]
    assert body["next_cursor"] is None


def test_get_events_filters_by_session_and_kind(http_client: TestClient) -> None:
    repo = JsonlSessionRepository()
    _write_session_events(
        repo, "sesn_a", [("agent.output", {"line": "a"}), ("session.status_idle", {})]
    )
    _write_session_events(repo, "sesn_b", [("agent.output", {"line": "b"})])

    response = http_client.get(
        "/v1/events", params={"session_id": "sesn_a", "kind": "agent.output"}
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["session_id"] == "sesn_a"
    assert events[0]["data"]["line"] == "a"


def test_get_events_paginates_with_next_cursor(http_client: TestClient) -> None:
    repo = JsonlSessionRepository()
    _write_session_events(repo, "sesn_a", [("agent.output", {"line": str(i)}) for i in range(5)])

    page1 = http_client.get("/v1/events", params={"limit": 2}).json()
    assert len(page1["events"]) == 2
    assert page1["next_cursor"] is not None

    page2 = http_client.get(
        "/v1/events", params={"limit": 2, "after_event_id": page1["next_cursor"]}
    ).json()
    assert len(page2["events"]) == 2
    assert page2["events"][0]["event_id"] != page1["events"][-1]["event_id"]


def test_get_events_filters_by_agent_via_session_created_resolution(
    http_client: TestClient,
) -> None:
    repo = JsonlSessionRepository()
    _write_session_events(repo, "sesn_a", [("session.created", {"agent": "claude_cli"})])
    _write_session_events(repo, "sesn_b", [("session.created", {"agent": "other"})])
    _write_session_events(repo, "sesn_a", [("agent.output", {"line": "from a"})])
    _write_session_events(repo, "sesn_b", [("agent.output", {"line": "from b"})])

    response = http_client.get("/v1/events", params={"agent": "claude_cli", "kind": "agent.output"})

    events = response.json()["events"]
    assert [e["session_id"] for e in events] == ["sesn_a"]


def test_get_events_rejects_limit_above_max(http_client: TestClient) -> None:
    response = http_client.get("/v1/events", params={"limit": 5000})
    assert response.status_code == 422  # FastAPI's ge/le validation


def test_get_events_rejects_invalid_uuid_cursor(http_client: TestClient) -> None:
    response = http_client.get("/v1/events", params={"after_event_id": "not-a-uuid"})
    assert response.status_code == 422


# ---- GET /v1/events/stream --------------------------------------------------


def test_parse_last_event_id_tolerates_missing_and_invalid() -> None:
    """An invalid ``Last-Event-ID`` (e.g. empty header sent by some SSE
    clients on first connect) must NOT abort the connection — it is
    treated as no catch-up. Tested at helper level because the live
    stream cannot be cleanly aborted via TestClient once headers are
    flushed; the route is a one-liner over this helper."""
    from uuid import uuid4

    from mad.adapters.inbound.http.routes.events import _parse_last_event_id

    assert _parse_last_event_id(None) is None
    assert _parse_last_event_id("") is None
    assert _parse_last_event_id("not-a-uuid") is None
    valid = uuid4()
    assert _parse_last_event_id(str(valid)) == valid


# ---- GET /v1/events/stream — heartbeat + anti-buffering (issue #34) ---------


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _make_event(session_id: str, type_: str, data: dict | None = None) -> Event:
    return Event(
        event_id=new_event_id(),
        session_id=session_id,
        type=type_,
        data=data or {},
        timestamp=_utcnow(),
    )


def _seq_event_id(seq: int) -> UUID:
    """Deterministic UUIDv7-shaped event_id ordered by ``seq``.

    Same trap as ``tests/integration/adapters/orchestration/test_projection.py``:
    ``FakeEventLogQuery`` lex-sorts on the event_id string, so within-millisecond
    UUIDv7 ordering is random (ADR-0005). Encoding ``seq`` into the timestamp
    prefix forces a stable replay order for the assertions below."""
    return UUID(int=(seq << 80) | (0x7 << 76) | (0b10 << 62))


async def _stream_with_injected_bus(
    bus: FakeEventBus,
    log: FakeEventLogQuery,
    *,
    headers: dict[str, str] | None = None,
    max_wait_s: float = 3.0,
) -> httpx.Response:
    """Run ``GET /v1/events/stream`` against an app where the event bus
    and log are test doubles. Bounds the source by trusting the caller
    to schedule ``bus.close_subscriber()`` (directly or via a publish
    burst). A hard ``max_wait_s`` guards against a buggy implementation
    that fails to honor the close signal — testing-heuristics rule 8
    (no test waits past the 15 s ``pytest-timeout`` cap)."""
    app = create_app(event_bus=bus, event_log_query=log)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await asyncio.wait_for(
            client.get("/v1/events/stream", headers=headers or {}),
            timeout=max_wait_s,
        )


async def _wait_for_subscription(bus: FakeEventBus, *, max_wait_s: float = 1.0) -> None:
    """Spin until ``bus.subscribe(...)`` has been called.

    Polls private state because adding a public ``subscribed`` event to
    the production port would leak test-only signaling into the contract.
    Bounded by ``max_wait_s`` so a regression in the request path fails
    loudly rather than hanging the suite (heuristic 8)."""
    deadline = asyncio.get_running_loop().time() + max_wait_s
    while bus._subscriber_queue is None:
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                "stream handler never called bus.subscribe() — request never reached the route"
            )
        await asyncio.sleep(0.005)


async def test_stream_emits_heartbeat_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no events on the bus, the stream must inject ``: ping\\n\\n``
    comment frames every ``MAD_SSE_HEARTBEAT_S``. Bounded by the
    subscriber close, which fires after ~4 heartbeat intervals so we
    can assert at least one frame WITHOUT relying on wall-clock drift."""
    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "0.05")
    bus = FakeEventBus()
    log = FakeEventLogQuery()

    async def closer() -> None:
        await _wait_for_subscription(bus)
        await asyncio.sleep(0.25)  # ~5 heartbeat intervals
        await bus.close_subscriber()

    closer_task = asyncio.create_task(closer())
    try:
        response = await _stream_with_injected_bus(bus, log)
    finally:
        if not closer_task.done():
            closer_task.cancel()

    assert response.status_code == 200
    # At least one heartbeat fired; no data frames since the bus was idle.
    assert response.text.count(": ping\n\n") >= 1
    assert "data:" not in response.text


async def test_stream_sets_anti_buffering_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cloudflare, nginx default, HAProxy, etc. buffer responses by
    default. The route must declare it does not want intermediary
    caching or transforms — failing this AC silently regresses every
    deployment behind a proxy."""
    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "10")  # large; not exercised here
    bus = FakeEventBus()
    log = FakeEventLogQuery()

    async def closer() -> None:
        await _wait_for_subscription(bus)
        await bus.close_subscriber()

    closer_task = asyncio.create_task(closer())
    try:
        response = await _stream_with_injected_bus(bus, log)
    finally:
        if not closer_task.done():
            closer_task.cancel()

    assert response.status_code == 200
    # Pinned values — proxy hints are contract, not "any non-default".
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-type"].startswith("text/event-stream")


async def test_stream_heartbeat_does_not_write_to_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-0004 / hard rule #8: heartbeats are transport-only. They
    must not call ``EventEmitter.emit()`` and therefore must not show
    up on the bus's published-events list (the emitter is the single
    write path per ADR-0007). Without this guard, a regression that
    adds e.g. a ``sse.heartbeat`` domain event would silently pollute
    the JSONL log."""
    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "0.04")
    bus = FakeEventBus()
    log = FakeEventLogQuery()

    async def closer() -> None:
        await _wait_for_subscription(bus)
        await asyncio.sleep(0.2)  # multiple heartbeat intervals
        await bus.close_subscriber()

    closer_task = asyncio.create_task(closer())
    try:
        response = await _stream_with_injected_bus(bus, log)
    finally:
        if not closer_task.done():
            closer_task.cancel()

    assert ": ping\n\n" in response.text  # confirm heartbeats DID fire
    assert bus.published == []  # but no domain event was emitted
    assert log.queries == []  # and the log was not touched (no agent filter)


async def test_stream_does_not_emit_heartbeat_when_events_flow_under_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative twin (heuristic 1): with events arriving faster than
    the heartbeat interval, NO heartbeat should fire. A naive
    implementation that emits ``: ping`` on every loop iteration —
    instead of only on idle timeout — would fail this test."""
    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "1.0")  # 1 s idle window
    bus = FakeEventBus()
    log = FakeEventLogQuery()

    async def publisher() -> None:
        await _wait_for_subscription(bus)
        for i in range(3):
            await bus.publish(_make_event("sesn_a", "agent.output", {"line": f"frame-{i}"}))
            await asyncio.sleep(0.05)  # well under the 1 s heartbeat window
        await bus.close_subscriber()

    pub_task = asyncio.create_task(publisher())
    try:
        response = await _stream_with_injected_bus(bus, log)
    finally:
        if not pub_task.done():
            pub_task.cancel()

    assert response.status_code == 200
    assert response.text.count("data:") == 3
    assert ": ping\n\n" not in response.text


async def test_stream_replay_via_last_event_id_unchanged_by_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heartbeat wrapper must not corrupt the ``Last-Event-ID``
    catch-up path: replayed events keep their ``id: <uuidv7>`` lines
    and arrive in order. Without this guard, a wrapper that consumed
    pending frames during a timeout could re-order or drop them."""
    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "10")  # never fires in this window
    bus = FakeEventBus()
    # ``last_id`` < ``earlier`` < ``later`` by construction so replay returns
    # exactly those two events in order. See ``_seq_event_id`` for the trap.
    last_id = _seq_event_id(1)
    earlier = Event(
        event_id=_seq_event_id(2),
        session_id="sesn_a",
        type="agent.output",
        data={"line": "early-1"},
        timestamp=_utcnow(),
    )
    later = Event(
        event_id=_seq_event_id(3),
        session_id="sesn_a",
        type="agent.output",
        data={"line": "early-2"},
        timestamp=_utcnow(),
    )
    log = FakeEventLogQuery(events=[earlier, later])

    async def closer() -> None:
        await _wait_for_subscription(bus)
        await bus.close_subscriber()

    closer_task = asyncio.create_task(closer())
    try:
        response = await _stream_with_injected_bus(
            bus,
            log,
            headers={"Last-Event-ID": str(last_id)},
        )
    finally:
        if not closer_task.done():
            closer_task.cancel()

    assert response.status_code == 200
    text = response.text
    # Both replayed events appear with their UUIDv7 id lines, in order.
    assert f"id: {earlier.event_id}\n" in text
    assert f"id: {later.event_id}\n" in text
    assert text.index(f"id: {earlier.event_id}") < text.index(f"id: {later.event_id}")
    assert ": ping\n\n" not in text  # heartbeat was never due in this window


# ---- _heartbeat_interval env-var resolution ---------------------------------


def test_heartbeat_interval_uses_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from mad.adapters.inbound.http.routes.events import (
        _HEARTBEAT_DEFAULT_S,
        _heartbeat_interval,
    )

    monkeypatch.delenv("MAD_SSE_HEARTBEAT_S", raising=False)
    assert _heartbeat_interval() == _HEARTBEAT_DEFAULT_S


def test_heartbeat_interval_falls_back_to_default_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-numeric env var must NOT disable the heartbeat — silently
    falling back to "no keepalive" would re-introduce the proxy stall
    this issue exists to prevent."""
    from mad.adapters.inbound.http.routes.events import (
        _HEARTBEAT_DEFAULT_S,
        _heartbeat_interval,
    )

    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "not-a-float")
    assert _heartbeat_interval() == _HEARTBEAT_DEFAULT_S


def test_heartbeat_interval_falls_back_to_default_on_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero would degenerate into a busy loop — coerce to default."""
    from mad.adapters.inbound.http.routes.events import (
        _HEARTBEAT_DEFAULT_S,
        _heartbeat_interval,
    )

    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "0")
    assert _heartbeat_interval() == _HEARTBEAT_DEFAULT_S


def test_heartbeat_interval_falls_back_to_default_on_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative interval is also degenerate — coerce to default rather
    than honoring a clearly-broken value (asserting this separately keeps
    failure attribution unambiguous if only one branch regresses)."""
    from mad.adapters.inbound.http.routes.events import (
        _HEARTBEAT_DEFAULT_S,
        _heartbeat_interval,
    )

    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "-5")
    assert _heartbeat_interval() == _HEARTBEAT_DEFAULT_S


def test_heartbeat_interval_honors_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from mad.adapters.inbound.http.routes.events import _heartbeat_interval

    monkeypatch.setenv("MAD_SSE_HEARTBEAT_S", "2.5")
    assert _heartbeat_interval() == pytest.approx(2.5)
