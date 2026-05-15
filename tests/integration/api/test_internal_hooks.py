"""Integration tests for #16: POST /_internal/hooks.

The internal app is intentionally separate from the public app so the
endpoint is unreachable from the public TCP bind. These tests cover:

- Happy path: a valid hook payload is persisted via EventEmitter and a 202
  response carries the assigned event_id.
- Negative twin: malformed type / missing session_id are rejected with 422.
- Token hygiene (CLAUDE.md hard rule 2): credential-shaped values inside
  ``data`` are scrubbed before emit.
- Defense-in-depth: the public app does NOT expose /_internal/hooks; the
  internal app does NOT expose /openapi.json or /docs.
- End-to-end: wire one shared EventEmitter into both apps and verify a
  hook posted on the internal app surfaces in the public GET /v1/events.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.adapters.inbound.internal.app import create_internal_app
from mad.core.events.emitter import EventEmitter
from support.events import FakeEventStore, RecordingEventBus


@pytest.fixture
def fake_store() -> FakeEventStore:
    return FakeEventStore()


@pytest.fixture
def fake_bus() -> RecordingEventBus:
    return RecordingEventBus()


@pytest.fixture
def emitter(fake_store: FakeEventStore, fake_bus: RecordingEventBus) -> EventEmitter:
    return EventEmitter(store=fake_store, bus=fake_bus)


@pytest.fixture
def internal_client(emitter: EventEmitter) -> TestClient:
    return TestClient(create_internal_app(emitter))


# ---- happy path -------------------------------------------------------------


def test_post_hook_returns_202_and_persists_through_emitter(
    internal_client: TestClient, fake_store: FakeEventStore, fake_bus: RecordingEventBus
) -> None:
    response = internal_client.post(
        "/_internal/hooks",
        json={
            "session_id": "sesn_abc",
            "type": "agent.claude_cli.hook.PreToolUse",
            "data": {"tool_name": "Bash"},
        },
    )

    assert response.status_code == 202, (
        f"expected 202, got {response.status_code} body={response.text!r}"
    )
    body = response.json()
    assert "event_id" in body, f"event_id missing from response: {body!r}"
    assert isinstance(body["event_id"], str), (
        f"event_id must be a string, got {type(body['event_id']).__name__}"
    )
    # Format pin: parses as UUID. The UUIDv7 version check is asserted in the
    # end-to-end test below, which wires the real JsonlSessionRepository
    # (FakeEventStore returns a fixed UUID for determinism).
    UUID(body["event_id"])

    # Single write-path assertion (CLAUDE.md hard rule 11): the store MUST
    # have received exactly one append, and the bus exactly one publish, with
    # matching session_id / type — not a mocked emit shortcut.
    assert len(fake_store.calls) == 1, f"expected 1 append, got {fake_store.calls}"
    sid, etype, data = fake_store.calls[0]
    assert sid == "sesn_abc"
    assert etype == "agent.claude_cli.hook.PreToolUse"
    assert data == {"tool_name": "Bash"}
    assert len(fake_bus.published) == 1, (
        f"expected 1 published event, got {len(fake_bus.published)}"
    )


# ---- negative twins (rule 1) ------------------------------------------------


def test_post_hook_rejects_malformed_type(internal_client: TestClient) -> None:
    """Type that does not match agent.<provider>.hook.<EventName> → 422."""
    response = internal_client.post(
        "/_internal/hooks",
        json={
            "session_id": "sesn_abc",
            "type": "session.created",  # wrong namespace, must be rejected
            "data": {},
        },
    )

    assert response.status_code == 422, (
        f"malformed type must yield 422, got {response.status_code}: {response.text!r}"
    )
    detail = response.json()["detail"]
    assert any(err["loc"][-1] == "type" for err in detail), (
        f"expected validation error on 'type' field, got: {detail}"
    )


def test_post_hook_rejects_missing_session_id(internal_client: TestClient) -> None:
    response = internal_client.post(
        "/_internal/hooks",
        json={"type": "agent.claude_cli.hook.PreToolUse", "data": {}},
    )

    assert response.status_code == 422, (
        f"missing session_id must yield 422, got {response.status_code}: {response.text!r}"
    )
    detail = response.json()["detail"]
    assert any(err["loc"][-1] == "session_id" for err in detail), (
        f"expected validation error on 'session_id', got: {detail}"
    )


def test_post_hook_rejects_empty_session_id(internal_client: TestClient) -> None:
    response = internal_client.post(
        "/_internal/hooks",
        json={
            "session_id": "",
            "type": "agent.claude_cli.hook.PreToolUse",
            "data": {},
        },
    )

    assert response.status_code == 422, (
        f"empty session_id must yield 422, got {response.status_code}: {response.text!r}"
    )


# ---- token hygiene (CLAUDE.md hard rule 2) ----------------------------------


def test_post_hook_scrubs_anthropic_token_inside_data(
    internal_client: TestClient, fake_store: FakeEventStore
) -> None:
    """An sk-ant-... value inside arbitrary string fields must be replaced
    with [REDACTED] before reaching the event log.
    """
    leaked = "sk-ant-api03-leaked-secret-XYZ123"
    response = internal_client.post(
        "/_internal/hooks",
        json={
            "session_id": "sesn_abc",
            "type": "agent.claude_cli.hook.PreToolUse",
            "data": {"command": f"curl -H 'Auth: {leaked}' https://api"},
        },
    )

    assert response.status_code == 202
    appended = fake_store.calls[0][2]
    assert appended is not None
    assert leaked not in str(appended), f"raw token leaked into persisted data: {appended!r}"
    assert "[REDACTED]" in appended["command"], (
        f"expected [REDACTED] in command, got: {appended['command']!r}"
    )


def test_post_hook_scrubs_credential_keys(
    internal_client: TestClient, fake_store: FakeEventStore
) -> None:
    """Values whose KEY matches a credential name must be replaced verbatim."""
    response = internal_client.post(
        "/_internal/hooks",
        json={
            "session_id": "sesn_abc",
            "type": "agent.claude_cli.hook.PreToolUse",
            "data": {
                "tool_name": "Bash",
                "token": "ghp_super_secret_1234567890",
                "nested": {"api_key": "sk-1234"},
            },
        },
    )

    assert response.status_code == 202
    appended = fake_store.calls[0][2]
    assert appended is not None
    assert appended["token"] == "[REDACTED]", f"top-level credential key not scrubbed: {appended!r}"
    assert appended["nested"]["api_key"] == "[REDACTED]", (
        f"nested credential key not scrubbed: {appended['nested']!r}"
    )
    # Non-credential field passes through unchanged
    assert appended["tool_name"] == "Bash"


# ---- defense in depth -------------------------------------------------------


def test_internal_app_does_not_expose_openapi(emitter: EventEmitter) -> None:
    client = TestClient(create_internal_app(emitter))
    spec_resp = client.get("/openapi.json")
    docs_resp = client.get("/docs")
    redoc_resp = client.get("/redoc")

    assert spec_resp.status_code == 404, (
        f"internal app exposed /openapi.json (status {spec_resp.status_code})"
    )
    assert docs_resp.status_code == 404, (
        f"internal app exposed /docs (status {docs_resp.status_code})"
    )
    assert redoc_resp.status_code == 404, (
        f"internal app exposed /redoc (status {redoc_resp.status_code})"
    )


def test_public_app_does_not_serve_internal_hooks_route(tmp_sessions_dir: Path) -> None:
    """The public app must not have /_internal/hooks mounted. A POST
    against the public app at this path must yield 404 / 405, never 202.
    """
    public = TestClient(create_app())

    response = public.post(
        "/_internal/hooks",
        json={"session_id": "sesn_x", "type": "agent.claude_cli.hook.PreToolUse"},
    )
    assert response.status_code == 404, (
        f"public app must return 404 for unmounted /_internal/hooks; got {response.status_code} "
        f"body={response.text!r}"
    )

    spec = public.get("/openapi.json").json()
    assert "/_internal/hooks" not in spec.get("paths", {}), (
        "public OpenAPI must not list /_internal/hooks"
    )


# ---- end-to-end with shared emitter ----------------------------------------


def test_hook_posted_on_internal_app_surfaces_in_public_get_events(
    tmp_sessions_dir: Path,
) -> None:
    """Wire the SAME EventEmitter into both apps (mirrors what
    entry_points/cli.py does in production) and verify a hook posted on
    the internal endpoint is queryable on the public GET /v1/events.
    """
    from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
    from mad.adapters.outbound.events.jsonl_event_log_query import JsonlEventLogQuery
    from mad.adapters.outbound.persistence.jsonl_session_repository import (
        JsonlSessionRepository,
    )

    repo = JsonlSessionRepository()
    bus = InMemoryEventBus()
    shared_emitter = EventEmitter(store=repo, bus=bus)

    public_app = create_app(
        session_repo=repo,
        event_bus=bus,
        event_log_query=JsonlEventLogQuery(),
        event_emitter=shared_emitter,
    )
    internal_app = create_internal_app(shared_emitter)

    internal = TestClient(internal_app)
    public = TestClient(public_app)

    post = internal.post(
        "/_internal/hooks",
        json={
            "session_id": "sesn_e2e",
            "type": "agent.claude_cli.hook.TaskCompleted",
            "data": {"task_id": "t-1"},
        },
    )
    assert post.status_code == 202, post.text

    listing = public.get("/v1/events", params={"session_id": "sesn_e2e"})
    assert listing.status_code == 200, listing.text
    events = listing.json()["events"]
    assert len(events) == 1, f"expected exactly 1 event, got {events!r}"
    event = events[0]
    assert event["type"] == "agent.claude_cli.hook.TaskCompleted"
    assert event["session_id"] == "sesn_e2e"
    assert event["data"]["task_id"] == "t-1"
    # ADR-0005: production EventStore must mint UUIDv7 event_ids.
    parsed = UUID(event["event_id"])
    assert parsed.version == 7, (
        f"event_id must be UUIDv7 (ADR-0005), got version={parsed.version} "
        f"value={event['event_id']!r}"
    )
