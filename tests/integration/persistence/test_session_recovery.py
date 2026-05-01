"""Session recovery tests — Hard rule 6 (JSONL as source of truth).

After a process restart, a new SessionStore + create_app() must be able to
serve GET /v1/sessions/{id} by reading the JSONL log, not from in-memory state.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.core.sessions import SessionStore


def test_get_session_reads_events_from_jsonl_after_restart(
    fake_launcher, session_payload: dict, tmp_sessions_dir
) -> None:
    """Create a session, discard the in-memory SessionStore, build a fresh app
    pointing at the same sessions/ directory, and verify that GET /v1/sessions/{id}
    returns the events from the JSONL log. (Hard rule 6 — recovery)
    """
    # --- First "process": create session, write to log ---
    first_app = create_app()
    first_client = TestClient(first_app)
    r = first_client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    # --- Simulate restart: new store, new app, same sessions/ dir ---
    new_store = SessionStore()
    second_app = create_app(store=new_store)
    second_client = TestClient(second_app)

    r2 = second_client.get(f"/v1/sessions/{session_id}")
    assert r2.status_code == 200, (
        f"After restart, GET /v1/sessions/{session_id} must return 200 by reading JSONL; "
        f"got {r2.status_code}"
    )
    events = r2.json().get("events", [])
    assert len(events) > 0, "Recovered session must have at least one event from the JSONL log"
    event_types = {e.get("type") for e in events}
    assert "session.created" in event_types, (
        f"Expected session.created in recovered events, got: {event_types}"
    )
