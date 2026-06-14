"""HTTP integration tests for ``PATCH /v1/sessions/{id}/priority`` (issue #46).

Contract under test: priority is an int in [1, 10] set only via this
endpoint (typed request/response); out-of-range input is rejected with a
422, never clamped; the setter emits ``dispatch_priority.updated``; the
value is readable on both session read endpoints and survives a process
restart via JSONL replay (no parallel store).

The conftest ``client`` fixture creates a TestClient WITHOUT the
lifespan context, so the dispatcher background task does not run —
deterministic, no real CLI.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app

# -- Helpers -------------------------------------------------------------------


def _create_session(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# -- PATCH /priority ------------------------------------------------------------


def test_patch_priority_round_trips(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 7})

    assert r.status_code == 200, r.text
    assert r.json() == {"session_id": session_id, "priority": 7}


def test_patch_priority_accepts_both_bounds(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)

    assert (
        client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 1}).json()["priority"]
        == 1
    )
    assert (
        client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 10}).json()[
            "priority"
        ]
        == 10
    )


def test_patch_priority_unknown_session_returns_404(client: TestClient) -> None:
    r = client.patch("/v1/sessions/sesn_missing/priority", json={"priority": 5})

    assert r.status_code == 404
    assert "sesn_missing" in r.json()["detail"]


@pytest.mark.parametrize("value", [0, 11, -5])
def test_patch_priority_out_of_range_returns_422(
    client: TestClient, session_payload: dict, value: int
) -> None:
    """Negative twin: out-of-range values are REJECTED at the boundary
    with a typed validation error pointing at the field — never clamped."""
    session_id = _create_session(client, session_payload)

    r = client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": value})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "priority"]


def test_patch_priority_non_int_returns_422(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": "high"})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "priority"]


def test_patch_priority_missing_body_field_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(f"/v1/sessions/{session_id}/priority", json={})

    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "priority"]


def test_patch_priority_emits_dispatch_priority_updated_event(
    client: TestClient, session_payload: dict
) -> None:
    """The event IS the durable record (hard rule 6) — it must land in the
    session log with the exact payload the replay reads."""
    session_id = _create_session(client, session_payload)

    client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 4})

    events = client.get(f"/v1/sessions/{session_id}").json()["events"]
    updated = [e for e in events if e["type"] == "dispatch_priority.updated"]
    assert len(updated) == 1
    assert updated[0]["priority"] == 4


# -- Readable priority ----------------------------------------------------------


def test_priority_defaults_to_one_in_session_detail(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.get(f"/v1/sessions/{session_id}")

    assert r.status_code == 200
    assert r.json()["priority"] == 1


def test_patched_priority_is_visible_in_session_detail_and_list(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 6})

    detail = client.get(f"/v1/sessions/{session_id}").json()
    assert detail["priority"] == 6

    listing = client.get("/v1/sessions").json()
    row = next(s for s in listing if s["session_id"] == session_id)
    assert row["priority"] == 6


# -- Restart / rehydration -------------------------------------------------------


def test_priority_survives_app_restart_via_log_replay(
    fake_launcher,
    tmp_sessions_dir,
    tmp_workspaces_dir,
    session_payload: dict,
) -> None:
    """No parallel store: a fresh app over the same sessions dir MUST
    reconstruct the priority from the replayed ``dispatch_priority.updated``
    event (mirrors the dispatch_policy restart contract)."""
    client_a = TestClient(create_app(launcher_factory=lambda name: fake_launcher))
    session_id = _create_session(client_a, session_payload)
    assert (
        client_a.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 9}).status_code
        == 200
    )
    client_a.close()

    client_b = TestClient(create_app(launcher_factory=lambda name: fake_launcher))
    r = client_b.get(f"/v1/sessions/{session_id}")
    assert r.status_code == 200
    assert r.json()["priority"] == 9
    client_b.close()


# -- OpenAPI contract (heuristic 5) ----------------------------------------------


def test_openapi_documents_the_priority_route(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    patch = spec["paths"]["/v1/sessions/{session_id}/priority"]["patch"]

    assert patch["requestBody"]["required"] is True
    body_schema = patch["requestBody"]["content"]["application/json"]["schema"]
    ref = body_schema["$ref"].rsplit("/", 1)[-1]
    component = spec["components"]["schemas"][ref]
    assert component["required"] == ["priority"]
    # The bounds are part of the contract: clients see them in /docs and
    # Postman, and 422 enforcement matches what is documented.
    assert component["properties"]["priority"]["minimum"] == 1
    assert component["properties"]["priority"]["maximum"] == 10

    response_ref = patch["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    response_schema = spec["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]
    assert sorted(response_schema["required"]) == ["priority", "session_id"]


def test_openapi_session_read_models_include_priority(client: TestClient) -> None:
    schemas = client.get("/openapi.json").json()["components"]["schemas"]

    assert schemas["SessionSummaryResponse"]["properties"]["priority"]["type"] == "integer"
    assert schemas["SessionDetailResponse"]["properties"]["priority"]["type"] == "integer"
