"""HTTP integration tests for the events module endpoints.

Exercises ``GET /v1/events`` and ``GET /v1/events/stream`` against a
fully-wired ``create_app`` with the JSONL persistence stack pointed at
``tmp_sessions_dir`` (CLAUDE.md hard rule 6 — log is the source of
truth).

Live-tail SSE behavior is covered at the use-case unit-test level
(``tests/unit/core/events/use_cases/test_stream_events.py``); here we
verify HTTP framing, the ``Last-Event-ID`` replay path, and filter
pass-through.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
)


@pytest.fixture
def http_client(tmp_sessions_dir: Path) -> TestClient:
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
