"""HTTP integration tests for the bulk session cleanup endpoint (issue #36).

Covers:
  - POST /v1/sessions/cleanup happy path (dry_run=false): deletes matching
    sessions, destroys workspaces, emits session.deleted, returns ids.
  - POST /v1/sessions/cleanup dry_run=true: reports candidates without
    mutating state or emitting events.
  - Status-agnostic selection: a "running" session with stale updated_at
    is deleted alongside the rest; no special skip.
  - Already-deleted tombstones are excluded from `examined` and not
    re-destroyed on a second cleanup call (idempotency on the tombstone set).
  - Future older_than returns 400 with the documented detail.
  - Naive / missing older_than returns 422 (Pydantic schema layer).
  - GET /v1/sessions hides deleted sessions by default; ?include_deleted=true
    surfaces them.
  - OpenAPI contract: request/response schemas and the include_deleted
    query param are declared in /openapi.json (heuristic 5).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient


def _create_session(client: TestClient, session_payload: dict) -> str:
    return client.post("/v1/sessions", json=session_payload).json()["session_id"]


def _force_old(client: TestClient, session_id: str, when: datetime) -> None:
    """Reset the in-memory entity's updated_at to a known past instant.

    The integration suite cannot wait minutes for sessions to age naturally;
    this is the test-only seam used to set up a cutoff scenario. Production
    code never mutates updated_at this way.
    """
    session = client.app.state.store.sessions[session_id]
    session.created_at = when
    session.updated_at = when


def _force_running(client: TestClient, session_id: str) -> None:
    client.app.state.store.sessions[session_id].status = "running"


# ---------------------------------------------------------------------------
# Happy path: dry_run=false deletes matching sessions
# ---------------------------------------------------------------------------


def test_cleanup_deletes_sessions_older_than_cutoff(
    client: TestClient, session_payload: dict, tmp_workspaces_dir: Path
) -> None:
    """A session with updated_at < older_than is destroyed: workspace gone,
    status==deleted, response carries the session_id in deleted_session_ids."""
    sid = _create_session(client, session_payload)
    workspace = Path(client.get(f"/v1/sessions/{sid}").json()["workspace"])
    assert workspace.exists(), "precondition: workspace must exist"

    _force_old(client, sid, datetime(2025, 1, 1, tzinfo=UTC))

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_session_ids"] == [sid]
    assert body["would_delete"] == []
    assert body["examined"] == 1
    assert not workspace.exists(), "workspace must be destroyed"


def test_cleanup_emits_session_deleted_with_final_status(
    client: TestClient, session_payload: dict, tmp_sessions_dir: Path
) -> None:
    """Per-session destroy emits session.deleted carrying final_status=prior
    status — same payload contract as DELETE /v1/sessions/{id}. Value-level
    assertion proves the bulk path reuses the destroy primitive."""
    sid = _create_session(client, session_payload)
    _force_old(client, sid, datetime(2025, 1, 1, tzinfo=UTC))

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text

    log_path = tmp_sessions_dir / f"{sid}.jsonl"
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    deleted_events = [e for e in events if e.get("type") == "session.deleted"]
    assert len(deleted_events) == 1, f"expected exactly one session.deleted, got {deleted_events}"
    assert deleted_events[0].get("final_status") == "created"


def test_cleanup_running_session_with_stale_updated_at_is_deleted(
    client: TestClient, session_payload: dict, tmp_sessions_dir: Path
) -> None:
    """Hard rule of this issue: no special skip for status=running. A
    session whose status is "running" but whose updated_at is older than
    the cutoff is destroyed; session.deleted carries final_status=running."""
    sid = _create_session(client, session_payload)
    _force_old(client, sid, datetime(2025, 1, 1, tzinfo=UTC))
    _force_running(client, sid)

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted_session_ids"] == [sid]

    log_path = tmp_sessions_dir / f"{sid}.jsonl"
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    deleted_events = [e for e in events if e.get("type") == "session.deleted"]
    assert deleted_events[0].get("final_status") == "running", (
        "stale running session must emit final_status=running, not be skipped"
    )


# ---------------------------------------------------------------------------
# Negative twin: sessions newer than the cutoff are NOT deleted
# ---------------------------------------------------------------------------


def test_cleanup_does_not_delete_sessions_newer_than_cutoff(
    client: TestClient, session_payload: dict
) -> None:
    """A session whose updated_at is >= older_than must survive: not in
    deleted_session_ids, workspace untouched, status not bumped to deleted."""
    sid = _create_session(client, session_payload)
    workspace = Path(client.get(f"/v1/sessions/{sid}").json()["workspace"])

    # Cutoff in the past, before any session could have been created.
    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2000-01-01T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_session_ids"] == []
    assert body["examined"] == 1
    assert workspace.exists(), "young session's workspace must be preserved"
    assert client.get(f"/v1/sessions/{sid}").json()["status"] != "deleted"


# ---------------------------------------------------------------------------
# dry_run=true: reports candidates without mutating state
# ---------------------------------------------------------------------------


def test_cleanup_dry_run_reports_candidates_without_destroying(
    client: TestClient, session_payload: dict, tmp_sessions_dir: Path
) -> None:
    """dry_run=true populates would_delete; workspace stays; no
    session.deleted event is appended to the JSONL log."""
    sid = _create_session(client, session_payload)
    workspace = Path(client.get(f"/v1/sessions/{sid}").json()["workspace"])
    _force_old(client, sid, datetime(2025, 1, 1, tzinfo=UTC))

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["would_delete"] == [sid]
    assert body["deleted_session_ids"] == []
    assert body["examined"] == 1
    assert workspace.exists(), "dry_run must NOT destroy the workspace"

    log_path = tmp_sessions_dir / f"{sid}.jsonl"
    events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    deleted_events = [e for e in events if e.get("type") == "session.deleted"]
    assert deleted_events == [], "dry_run must NOT emit session.deleted"
    assert client.get(f"/v1/sessions/{sid}").json()["status"] != "deleted"


# ---------------------------------------------------------------------------
# Tombstones: already-deleted sessions excluded from examined; idempotency
# ---------------------------------------------------------------------------


def test_cleanup_excludes_already_deleted_from_examined(
    client: TestClient, session_payload: dict
) -> None:
    """A session already at status=deleted is invisible to cleanup: not
    counted in examined, not re-destroyed, not echoed in deleted_session_ids."""
    sid = _create_session(client, session_payload)
    _force_old(client, sid, datetime(2025, 1, 1, tzinfo=UTC))
    # First call destroys it.
    client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": False},
    )

    # Second call must treat it as if it didn't exist.
    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted_session_ids"] == []
    assert body["examined"] == 0
    assert sid not in body["deleted_session_ids"]


# ---------------------------------------------------------------------------
# Negative: future older_than (400 with documented detail)
# ---------------------------------------------------------------------------


def test_cleanup_rejects_future_older_than_with_400(
    client: TestClient, session_payload: dict
) -> None:
    """older_than > now() returns 400 with detail "older_than is not valid".
    Semantic check (not Pydantic schema), so it's 400 — distinct from the
    422 path that signals malformed input."""
    _create_session(client, session_payload)
    far_future = (datetime.now(UTC) + timedelta(days=365)).isoformat()

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": far_future, "dry_run": False},
    )
    assert r.status_code == 400, r.text
    assert r.json() == {"detail": "older_than is not valid"}


def test_cleanup_future_older_than_does_not_destroy_anything(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin to the 400: the request being rejected must mean NO
    side effects — the workspace is intact, the session not deleted."""
    sid = _create_session(client, session_payload)
    workspace = Path(client.get(f"/v1/sessions/{sid}").json()["workspace"])
    far_future = (datetime.now(UTC) + timedelta(days=365)).isoformat()

    client.post(
        "/v1/sessions/cleanup",
        json={"older_than": far_future, "dry_run": False},
    )

    assert workspace.exists(), "rejected request must not destroy state"
    assert client.get(f"/v1/sessions/{sid}").json()["status"] != "deleted"


# ---------------------------------------------------------------------------
# Negative: Pydantic schema layer (422)
# ---------------------------------------------------------------------------


def test_cleanup_rejects_missing_older_than_with_422(client: TestClient) -> None:
    """Missing older_than returns 422 from Pydantic; the detail names
    body.older_than so clients can render the offending field."""
    r = client.post("/v1/sessions/cleanup", json={"dry_run": False})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list) and len(detail) >= 1
    assert any(d.get("loc") == ["body", "older_than"] for d in detail), (
        f"422 detail must point at body.older_than; got {detail}"
    )


def test_cleanup_rejects_garbage_older_than_with_422(client: TestClient) -> None:
    """A non-ISO string is 422 (Pydantic schema validation) — NOT 400.
    The 400 path is reserved for the semantic "future cutoff" check."""
    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "not-a-date", "dry_run": False},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert any(d.get("loc") == ["body", "older_than"] for d in detail)


# ---------------------------------------------------------------------------
# GET /v1/sessions: include_deleted default + opt-in
# ---------------------------------------------------------------------------


def test_list_sessions_hides_deleted_by_default(client: TestClient, session_payload: dict) -> None:
    """After a session is deleted, GET /v1/sessions must not include it
    unless include_deleted=true is supplied."""
    sid = _create_session(client, session_payload)
    client.delete(f"/v1/sessions/{sid}")

    r = client.get("/v1/sessions")
    assert r.status_code == 200
    ids = [s["session_id"] for s in r.json()]
    assert sid not in ids, f"default listing must hide deleted; got {ids}"


def test_list_sessions_include_deleted_true_surfaces_tombstones(
    client: TestClient, session_payload: dict
) -> None:
    """include_deleted=true preserves today's behavior — deleted sessions
    appear in the listing for audit / operator-debug use cases."""
    sid = _create_session(client, session_payload)
    client.delete(f"/v1/sessions/{sid}")

    r = client.get("/v1/sessions", params={"include_deleted": "true"})
    assert r.status_code == 200
    matching = [s for s in r.json() if s["session_id"] == sid]
    assert len(matching) == 1, f"include_deleted=true must surface tombstone; got {r.json()}"
    assert matching[0]["status"] == "deleted"


def test_list_sessions_include_deleted_false_still_shows_live(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin: filtering out tombstones must not hide live sessions.
    Default + a live session = listing contains the live session."""
    live = _create_session(client, session_payload)
    deleted = _create_session(client, session_payload)
    client.delete(f"/v1/sessions/{deleted}")

    r = client.get("/v1/sessions")
    assert r.status_code == 200
    ids = [s["session_id"] for s in r.json()]
    assert live in ids
    assert deleted not in ids


# ---------------------------------------------------------------------------
# OpenAPI contract (heuristic 5 — every POST JSON endpoint)
# ---------------------------------------------------------------------------


def _resolve_ref(spec: dict, ref: str) -> dict:
    name = ref.rsplit("/", 1)[-1]
    return spec["components"]["schemas"][name]


def test_openapi_cleanup_declares_required_body_schema(client: TestClient) -> None:
    """POST /v1/sessions/cleanup must declare a required body whose schema
    marks older_than as required and types dry_run as boolean (rule 5)."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions/cleanup"]["post"]
    body = op["requestBody"]
    assert body["required"] is True

    schema_ref = body["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    required = set(component.get("required", []))
    assert "older_than" in required, (
        f"CleanupSessionsRequest must require older_than; got required={required}"
    )
    older_than_type = component["properties"]["older_than"]
    assert older_than_type.get("format") == "date-time"
    dry_run_type = component["properties"]["dry_run"]
    assert dry_run_type.get("type") == "boolean"


def test_openapi_cleanup_declares_response_model(client: TestClient) -> None:
    """The 200 response must reference CleanupSessionsResponse with the
    documented field set so clients can rely on the contract."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions/cleanup"]["post"]
    schema_ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    props = component["properties"]
    assert set(props.keys()) >= {"deleted_session_ids", "would_delete", "examined"}
    assert props["deleted_session_ids"]["type"] == "array"
    assert props["would_delete"]["type"] == "array"
    assert props["examined"]["type"] == "integer"


def test_openapi_declares_include_deleted_query_param(client: TestClient) -> None:
    """GET /v1/sessions must expose ?include_deleted as a documented query
    param so it appears in /docs and Postman alongside the existing filters."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions"]["get"]
    names = {p["name"]: p for p in op.get("parameters", [])}
    assert "include_deleted" in names, (
        f"OpenAPI does not declare ?include_deleted; declared: {sorted(names)}"
    )
    assert names["include_deleted"]["in"] == "query"
    assert names["include_deleted"]["schema"].get("type") == "boolean"


# ---------------------------------------------------------------------------
# Disk-rehydration parity with GET /v1/sessions (post-server-restart case)
# ---------------------------------------------------------------------------


def test_cleanup_rehydrates_disk_only_session_from_jsonl(
    client: TestClient, tmp_sessions_dir: Path
) -> None:
    """A session that exists only as a JSONL log on disk (the common
    post-server-restart state) is rehydrated by the cleanup endpoint
    via SessionRepository — the same source GET /v1/sessions reads
    from. This is the bug surfaced manually after PR #37 merged: the
    list endpoint showed dozens of sessions, but cleanup reported
    examined=1 because it only iterated the in-memory index.
    """
    disk_sid = "sesn_disk_only_for_cleanup_test"
    old_ts = "2025-01-01T00:00:00+00:00"
    log_path = tmp_sessions_dir / f"{disk_sid}.jsonl"
    log_path.write_text(
        json.dumps({"type": "session.created", "session_id": disk_sid, "timestamp": old_ts})
        + "\n"
        + json.dumps({"type": "session.status_idle", "session_id": disk_sid, "timestamp": old_ts})
        + "\n"
    )
    assert disk_sid not in client.app.state.store.sessions, (
        "precondition: the disk-only session must NOT be in the in-memory index"
    )
    assert client.app.state.store.sessions == {}, (
        "precondition: no other sessions in this test — examined and would_delete are pinned"
    )

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["would_delete"] == [disk_sid]
    assert body["deleted_session_ids"] == []
    assert body["examined"] == 1


def test_cleanup_listing_and_cleanup_universes_agree(
    client: TestClient, tmp_sessions_dir: Path
) -> None:
    """Negative twin to the integration above and a regression test for
    the listing-vs-cleanup mismatch: every session ``GET /v1/sessions``
    surfaces must appear in ``examined`` (or be a tombstone). Without
    rehydration the cleanup `examined` count was a strict subset of the
    listing, which is exactly the operator pain that motivated this fix.
    """
    disk_sid = "sesn_disk_only_for_parity_test"
    old_ts = "2025-01-01T00:00:00+00:00"
    log_path = tmp_sessions_dir / f"{disk_sid}.jsonl"
    log_path.write_text(
        json.dumps({"type": "session.created", "session_id": disk_sid, "timestamp": old_ts})
        + "\n"
        + json.dumps({"type": "session.status_idle", "session_id": disk_sid, "timestamp": old_ts})
        + "\n"
    )

    listing_ids = {s["session_id"] for s in client.get("/v1/sessions").json()}
    assert listing_ids == {disk_sid}, (
        f"precondition: only the disk session is present; got {listing_ids}"
    )

    r = client.post(
        "/v1/sessions/cleanup",
        json={"older_than": "2025-06-01T00:00:00Z", "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Set parity: every session the (non-tombstone) listing surfaces also
    # appears in the cleanup universe. This is the regression-guard for
    # the listing-vs-cleanup mismatch this fix addresses.
    assert set(body["would_delete"]) == listing_ids
    assert body["examined"] == len(listing_ids)
    assert body["deleted_session_ids"] == []
