"""Acceptance tests mapped 1:1 to specs/infra/requirements.md → MVP acceptance criteria.

Acceptance criterion → test mapping:
  AC-1  POST /v1/sessions with a GitHub repo → repo cloned in workspace → test_mvp_01_*
  AC-1b POST /v1/sessions provisions file resource                       → test_mvp_01b_*
  AC-2  POST /v1/sessions/{id}/events sends user.message                → test_mvp_02_*
  AC-2b POST /v1/sessions/{id}/events is non-blocking                   → test_mvp_02b_*
  AC-3b stream emits session.status_running and session.status_idle     → test_mvp_03b_*
  AC-4  GET /v1/sessions/{id} returns final state + every event         → test_mvp_04_*
  AC-4b JSONL session log records every event                           → test_mvp_04b_*
  AC-5  GET /v1/sessions lists past sessions                            → test_mvp_05_*
  AC-6  Resume session with second user.message                         → test_mvp_06_*
  AC-6b Resumed session log contains both turns                         → test_mvp_06b_*
  AC-7  DELETE cleans workspace, preserves log                          → test_mvp_07_*
  AC-8  Idempotency-Key returns same session, no double-clone           → test_mvp_08_*

These tests are EXPECTED to fail until the implementer runs (red TDD state).
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# AC-1: POST /v1/sessions with a GitHub repo → repo cloned in workspace
# Covers FR-1, FR-2 (github_repository)
# ---------------------------------------------------------------------------

def test_mvp_01_create_session_clones_repo(
    client: TestClient, session_payload: dict
) -> None:
    """POST /v1/sessions returns session_id, status=created, and the repo is on disk."""
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200
    data = r.json()

    assert "session_id" in data
    assert data["status"] == "created"
    assert "workspace" in data

    assert len(data["resources_mounted"]) == 1
    mounted = data["resources_mounted"][0]
    assert mounted["type"] == "github_repository"
    assert mounted["status"] == "cloned"
    assert "local_path" in mounted
    local = Path(mounted["local_path"])
    assert local.exists(), f"cloned repo must exist at {local}"
    # The repo must have a .git directory (it is a working checkout, not bare)
    assert (local / ".git").exists(), "cloned directory must be a git working tree"
    # The README seeded into the bare repo must be present
    assert (local / "README.md").exists(), "seeded README must be present after clone"


def test_mvp_01_create_session_response_shape(
    client: TestClient, session_payload: dict
) -> None:
    """POST /v1/sessions response carries all fields described in api.md."""
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200
    data = r.json()
    assert data["session_id"].startswith("sesn_") or len(data["session_id"]) > 0
    workspace = Path(data["workspace"])
    assert workspace.exists()


# ---------------------------------------------------------------------------
# AC-1b: POST /v1/sessions with a file resource → file written into workspace
# Covers FR-2 (file type)
# ---------------------------------------------------------------------------

def test_mvp_01b_create_session_provisions_file_resource(
    client: TestClient, fake_launcher
) -> None:
    """A resource of type=file must be written to the mapped mount_path."""
    payload = {
        "agent": {"name": "file-agent", "system": "test", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "file",
                "content": "hello from test\n",
                "mount_path": "/workspace/data/input.txt",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    mounted = data["resources_mounted"][0]
    assert mounted["type"] == "file"
    local = Path(mounted["local_path"])
    assert local.exists(), "file resource must be written to disk"
    assert local.read_text() == "hello from test\n"


def test_mvp_01b_mixed_resources_provisioned(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """Session with both a github_repository and a file resource provisions both."""
    payload = {
        "agent": {"name": "mixed-agent", "system": "test", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
                "authorization_token": "ghp_fake",
            },
            {
                "type": "file",
                "content": "task description\n",
                "mount_path": "/workspace/task.md",
            },
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert len(data["resources_mounted"]) == 2
    statuses = {m["type"]: m for m in data["resources_mounted"]}
    assert statuses["github_repository"]["status"] == "cloned"
    assert Path(statuses["file"]["local_path"]).read_text() == "task description\n"


# ---------------------------------------------------------------------------
# AC-2: POST /v1/sessions/{id}/events sends a user.message
# Covers FR-4
# ---------------------------------------------------------------------------

def test_mvp_02_send_user_message_starts_agent(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """POST /v1/sessions/{id}/events with user.message returns 200/202."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]

    r = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "hello"}]},
    )
    assert r.status_code in (200, 202)


def test_mvp_02_send_event_to_unknown_session_returns_404(
    client: TestClient, fake_launcher
) -> None:
    """Sending an event to a non-existent session must return 404."""
    r = client.post(
        "/v1/sessions/sesn_doesnotexist/events",
        json={"events": [{"type": "user.message", "content": "hi"}]},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# AC-2b: POST /v1/sessions/{id}/events is non-blocking
# Covers FR-6 (background agent launch)
# ---------------------------------------------------------------------------

def test_mvp_02b_send_event_is_non_blocking(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """The events endpoint returns immediately; the agent runs in the background."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]

    start = time.monotonic()
    r = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "go"}]},
    )
    elapsed = time.monotonic() - start
    assert r.status_code in (200, 202)
    # The endpoint must return in well under 5 seconds — if the agent launch were
    # blocking, a slow launcher would stall the response.
    assert elapsed < 5.0, f"events endpoint took {elapsed:.2f}s — must be non-blocking"


# ---------------------------------------------------------------------------
# AC-3b: SSE stream emits session.status_running and session.status_idle
# (AC-3 / test_mvp_03_stream_emits_agent_events is REMOVED: agent.tool_use
#  events no longer exist in the infrastructure-only model.)
# Covers FR-5, FR-6
# ---------------------------------------------------------------------------

def test_mvp_03b_stream_emits_lifecycle_events(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """Stream must emit session.status_running when launch starts and session.status_idle when done."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "work"}]},
    )

    with client.stream("GET", f"/v1/sessions/{session_id}/stream") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers.get("content-type", "")
        collected: list[dict] = []
        for line in r.iter_lines():
            if line.startswith("data:"):
                payload_str = line[len("data:"):].strip()
                try:
                    collected.append(json.loads(payload_str))
                except json.JSONDecodeError:
                    pass
            if any(e.get("type") == "session.status_idle" for e in collected):
                break

    event_types = {e["type"] for e in collected}
    assert "session.status_running" in event_types, (
        f"expected session.status_running, got: {event_types}"
    )
    assert "session.status_idle" in event_types, (
        f"expected session.status_idle, got: {event_types}"
    )
    # The idle event must carry a stop_reason
    idle_events = [e for e in collected if e.get("type") == "session.status_idle"]
    assert idle_events[0].get("stop_reason") is not None


def test_mvp_03b_stream_unknown_session_returns_404(client: TestClient) -> None:
    """GET /v1/sessions/unknown/stream must return 404, not 500."""
    r = client.get("/v1/sessions/sesn_doesnotexist/stream")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# AC-4: GET /v1/sessions/{id} returns final state with every event recorded
# Covers FR-8
# ---------------------------------------------------------------------------

def test_mvp_04_get_session_returns_final_state(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """GET /v1/sessions/{id} must return status and event list after agent finishes."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "go"}]},
    )

    r = client.get(f"/v1/sessions/{session_id}")
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert "events" in data
    assert isinstance(data["events"], list)


def test_mvp_04_get_session_events_include_user_message(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """The event list must include the user.message event that was sent."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "my unique task request"}]},
    )

    r = client.get(f"/v1/sessions/{session_id}")
    assert r.status_code == 200
    events = r.json()["events"]
    user_messages = [e for e in events if e.get("type") == "user.message"]
    assert len(user_messages) >= 1
    contents = [e.get("content", "") for e in user_messages]
    assert any("my unique task request" in c for c in contents)


def test_mvp_04_get_unknown_session_returns_404(client: TestClient) -> None:
    """GET /v1/sessions/unknown must return 404."""
    r = client.get("/v1/sessions/sesn_doesnotexist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# AC-4b: JSONL session log records every event
# Covers FR-7, NFR-3
# ---------------------------------------------------------------------------

def test_mvp_04b_jsonl_log_is_created_on_session_start(
    client: TestClient, session_payload: dict
) -> None:
    """A JSONL log file at sessions/{session_id}.jsonl must exist after session creation."""
    r = client.post("/v1/sessions", json=session_payload)
    session_id = r.json()["session_id"]

    log_path = Path("sessions") / f"{session_id}.jsonl"
    assert log_path.exists(), f"session log must exist at {log_path}"


def test_mvp_04b_jsonl_log_records_agent_events(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """After the agent finishes, the JSONL log must contain session.created and user.message."""
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    r = client.post("/v1/sessions", json=session_payload)
    session_id = r.json()["session_id"]
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "record me"}]},
    )

    log_path = Path("sessions") / f"{session_id}.jsonl"
    assert log_path.exists()
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    event_types = {e["type"] for e in lines}
    assert "session.created" in event_types, f"log missing session.created; got {event_types}"
    assert "user.message" in event_types, f"log missing user.message; got {event_types}"


def test_mvp_04b_jsonl_log_each_line_is_valid_json(
    client: TestClient, session_payload: dict
) -> None:
    """Every line in the JSONL log must be valid JSON with a 'type' field."""
    r = client.post("/v1/sessions", json=session_payload)
    session_id = r.json()["session_id"]
    log_path = Path("sessions") / f"{session_id}.jsonl"
    for i, line in enumerate(log_path.read_text().splitlines()):
        if not line.strip():
            continue
        parsed = json.loads(line)  # must not raise
        assert "type" in parsed, f"line {i} missing 'type' field: {line}"


# ---------------------------------------------------------------------------
# AC-5: GET /v1/sessions lists past sessions
# Covers FR-8
# ---------------------------------------------------------------------------

def test_mvp_05_list_sessions_returns_list(
    client: TestClient, session_payload: dict
) -> None:
    """GET /v1/sessions must return a JSON array (or an object with a 'sessions' key)."""
    r = client.get("/v1/sessions")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list) or "sessions" in body


def test_mvp_05_list_sessions_includes_created_session(
    client: TestClient, session_payload: dict
) -> None:
    """After creating a session, GET /v1/sessions must include that session_id."""
    created = client.post("/v1/sessions", json=session_payload).json()
    session_id = created["session_id"]

    r = client.get("/v1/sessions")
    assert r.status_code == 200
    body = r.json()
    sessions = body if isinstance(body, list) else body["sessions"]

    ids = [
        s["session_id"] if isinstance(s, dict) else s
        for s in sessions
    ]
    assert session_id in ids, f"session {session_id} not found in listing: {ids}"


# ---------------------------------------------------------------------------
# AC-6: Resume session by sending a second user.message to the same session_id
# Covers FR-4, FR-6
# ---------------------------------------------------------------------------

def test_mvp_06_resume_session_with_new_message(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """A second user.message to the same session must be accepted and processed."""
    fake_launcher.script([
        [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        [{"type": "session.status_idle", "stop_reason": "end_turn"}],
    ])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]

    r1 = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "first task"}]},
    )
    r2 = client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "second task"}]},
    )
    assert r1.status_code in (200, 202)
    assert r2.status_code in (200, 202)


# ---------------------------------------------------------------------------
# AC-6b: Resumed session log contains both turns
# Covers FR-7 (log is source of truth across turns)
# ---------------------------------------------------------------------------

def test_mvp_06b_resumed_session_log_contains_both_turns(
    client: TestClient, fake_launcher, session_payload: dict
) -> None:
    """The JSONL log must record user.message events from both turns after a resume."""
    fake_launcher.script([
        [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        [{"type": "session.status_idle", "stop_reason": "end_turn"}],
    ])
    session_id = client.post("/v1/sessions", json=session_payload).json()["session_id"]

    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "alpha task"}]},
    )
    client.post(
        f"/v1/sessions/{session_id}/events",
        json={"events": [{"type": "user.message", "content": "beta task"}]},
    )

    log_path = Path("sessions") / f"{session_id}.jsonl"
    lines = [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]
    user_messages = [e for e in lines if e.get("type") == "user.message"]
    contents = [e.get("content", "") for e in user_messages]
    assert any("alpha task" in c for c in contents), "first turn message missing from log"
    assert any("beta task" in c for c in contents), "second turn message missing from log"


# ---------------------------------------------------------------------------
# AC-7: DELETE /v1/sessions/{id} cleans workspace, preserves log
# Covers FR-8
# ---------------------------------------------------------------------------

def test_mvp_07_delete_cleans_workspace_preserves_log(
    client: TestClient, session_payload: dict
) -> None:
    """DELETE must remove the workspace directory and keep the JSONL log."""
    r = client.post("/v1/sessions", json=session_payload)
    data = r.json()
    session_id = data["session_id"]
    workspace = Path(data["workspace"])
    assert workspace.exists(), "workspace must exist before delete"

    r = client.delete(f"/v1/sessions/{session_id}")
    assert r.status_code in (200, 204)
    assert not workspace.exists(), "workspace directory must be removed after DELETE"

    log_path = Path("sessions") / f"{session_id}.jsonl"
    assert log_path.exists(), "session log must be preserved after DELETE"


def test_mvp_07_delete_unknown_session_returns_404(client: TestClient) -> None:
    """DELETE /v1/sessions/unknown must return 404."""
    r = client.delete("/v1/sessions/sesn_doesnotexist")
    assert r.status_code == 404


def test_mvp_07_get_after_delete_returns_404_or_deleted_status(
    client: TestClient, session_payload: dict
) -> None:
    """After DELETE, GET /v1/sessions/{id} should return 404 or status=deleted."""
    r = client.post("/v1/sessions", json=session_payload)
    session_id = r.json()["session_id"]
    client.delete(f"/v1/sessions/{session_id}")

    r = client.get(f"/v1/sessions/{session_id}")
    if r.status_code == 200:
        assert r.json().get("status") in ("deleted", "closed"), (
            "After DELETE, GET must return 404 or status=deleted/closed"
        )
    else:
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# AC-8: Idempotency-Key returns same session, no double-clone
# Covers FR-9
# ---------------------------------------------------------------------------

def test_mvp_08_idempotency_key_returns_same_session(
    client: TestClient, session_payload: dict
) -> None:
    """Two POST /v1/sessions with the same Idempotency-Key must return the same session_id."""
    headers = {"Idempotency-Key": "11111111-2222-3333-4444-555555555555"}
    r1 = client.post("/v1/sessions", json=session_payload, headers=headers)
    r2 = client.post("/v1/sessions", json=session_payload, headers=headers)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["session_id"] == r2.json()["session_id"], (
        "Replayed Idempotency-Key must return the existing session"
    )


def test_mvp_08_idempotency_key_does_not_reclone(
    client: TestClient, session_payload: dict
) -> None:
    """The second request with the same Idempotency-Key must NOT create a second workspace."""
    headers = {"Idempotency-Key": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}
    r1 = client.post("/v1/sessions", json=session_payload, headers=headers)
    workspace1 = Path(r1.json()["workspace"])

    r2 = client.post("/v1/sessions", json=session_payload, headers=headers)
    workspace2 = Path(r2.json()["workspace"])

    assert workspace1 == workspace2, (
        "Second idempotent request must return the same workspace path"
    )


def test_mvp_08_different_idempotency_keys_create_different_sessions(
    client: TestClient, session_payload: dict
) -> None:
    """Different Idempotency-Key values must produce different sessions."""
    r1 = client.post(
        "/v1/sessions",
        json=session_payload,
        headers={"Idempotency-Key": "key-alpha-0001"},
    )
    r2 = client.post(
        "/v1/sessions",
        json=session_payload,
        headers={"Idempotency-Key": "key-beta-0002"},
    )
    assert r1.json()["session_id"] != r2.json()["session_id"]
