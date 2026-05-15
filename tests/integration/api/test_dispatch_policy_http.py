"""HTTP integration tests for dispatch-policy routes (issue #33).

Endpoints under test (per ADR-0009 §9):
- PATCH /v1/sessions/{session_id}/dispatch_policy
- POST  /v1/sessions/{session_id}/dispatch_policy/trigger

The conftest ``client`` fixture creates a TestClient *without* the
lifespan context, so the dispatcher background task does NOT run —
that keeps these tests deterministic. Tests that need queued state
inject directly into ``app.state.task_projection`` as the existing
orchestration_http suite does.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.core.orchestration.domain.task import Task

# -- Helpers -------------------------------------------------------------------


def _create_session(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _inject_queued(client: TestClient, session_id: str, *, content: str = "queued") -> Task:
    projection = client.app.state.task_projection
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    projection._queued.setdefault(session_id, []).append(task)
    return task


# -- PATCH /dispatch_policy ---------------------------------------------------


def test_patch_dispatch_policy_immediate_round_trips(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={"kind": "immediate"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"] == session_id
    assert body["policy"] == {"kind": "immediate"}


def test_patch_dispatch_policy_manual_round_trips(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={"kind": "manual"},
    )

    assert r.status_code == 200
    assert r.json()["policy"] == {"kind": "manual"}


def test_patch_dispatch_policy_work_window_serializes_full_shape(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={
            "kind": "work_window",
            "windows": [
                {
                    "start": "18:00",
                    "end": "08:00",
                    "timezone": "America/Mexico_City",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                }
            ],
        },
    )

    assert r.status_code == 200
    policy = r.json()["policy"]
    assert policy["kind"] == "work_window"
    assert len(policy["windows"]) == 1
    w = policy["windows"][0]
    assert w["start"] == "18:00"
    assert w["end"] == "08:00"
    assert w["timezone"] == "America/Mexico_City"
    assert sorted(w["days"]) == ["fri", "mon", "thu", "tue", "wed"]


def test_patch_dispatch_policy_unknown_session_returns_404(client: TestClient) -> None:
    r = client.patch(
        "/v1/sessions/sesn_missing/dispatch_policy",
        json={"kind": "immediate"},
    )
    assert r.status_code == 404


def test_patch_dispatch_policy_unknown_timezone_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    """An IANA timezone Pydantic accepts as a string but ``zoneinfo``
    rejects MUST surface as 422 — operator typos belong on the caller's
    side, not in a 500."""
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={
            "kind": "work_window",
            "windows": [{"start": "18:00", "end": "08:00", "timezone": "Atlantis/Capital"}],
        },
    )

    assert r.status_code == 422
    assert "Atlantis/Capital" in r.json()["detail"]


def test_patch_dispatch_policy_malformed_hhmm_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={
            "kind": "work_window",
            "windows": [{"start": "1800", "end": "08:00", "timezone": "America/Mexico_City"}],
        },
    )

    assert r.status_code == 422


def test_patch_dispatch_policy_empty_windows_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    """Pydantic ``min_length=1`` defends against zero-window payloads
    BEFORE the domain validator sees them — confirms the discriminated
    union is wired correctly."""
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={"kind": "work_window", "windows": []},
    )

    assert r.status_code == 422


def test_patch_dispatch_policy_unknown_kind_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={"kind": "schedule"},
    )

    assert r.status_code == 422


# -- POST /dispatch_policy/trigger --------------------------------------------


def test_post_trigger_in_manual_mode_returns_202_with_drained_count(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    # Switch to manual.
    client.patch(f"/v1/sessions/{session_id}/dispatch_policy", json={"kind": "manual"})
    # Inject 2 queued tasks so the trigger has something to count.
    _inject_queued(client, session_id, content="A")
    _inject_queued(client, session_id, content="B")

    r = client.post(f"/v1/sessions/{session_id}/dispatch_policy/trigger")

    assert r.status_code == 202, r.text
    assert r.json() == {"session_id": session_id, "drained": 2}


def test_post_trigger_in_immediate_mode_returns_409(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin: in ``immediate`` mode the dispatcher already
    fires autonomously, so an explicit trigger is misconfiguration —
    409 over silent no-op so the operator notices."""
    session_id = _create_session(client, session_payload)
    # Default policy is ``immediate`` — no PATCH needed.

    r = client.post(f"/v1/sessions/{session_id}/dispatch_policy/trigger")

    assert r.status_code == 409
    assert "immediate" in r.json()["detail"]


def test_post_trigger_in_work_window_mode_returns_409(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    client.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={
            "kind": "work_window",
            "windows": [{"start": "18:00", "end": "08:00", "timezone": "America/Mexico_City"}],
        },
    )

    r = client.post(f"/v1/sessions/{session_id}/dispatch_policy/trigger")

    assert r.status_code == 409
    assert "work_window" in r.json()["detail"]


def test_post_trigger_unknown_session_returns_404(client: TestClient) -> None:
    r = client.post("/v1/sessions/sesn_missing/dispatch_policy/trigger")
    assert r.status_code == 404


# -- Restart / rehydration ----------------------------------------------------


def test_dispatch_policy_survives_app_restart_via_log_replay(
    fake_launcher,
    tmp_sessions_dir,
    tmp_workspaces_dir,
    session_payload: dict,
) -> None:
    """Switching to manual emits ``dispatch_policy.updated``; the JSONL
    log is the source of truth (hard rule 6). A new app instance against
    the same sessions dir MUST rehydrate the policy — otherwise an
    operator-set policy would be lost on every redeploy."""
    # First boot: create a session and switch it to manual.
    client_a = TestClient(create_app(launcher_factory=lambda name: fake_launcher))
    session_id = _create_session(client_a, session_payload)
    r = client_a.patch(
        f"/v1/sessions/{session_id}/dispatch_policy",
        json={"kind": "manual"},
    )
    assert r.status_code == 200
    client_a.close()

    # Second boot: fresh app, same sessions_dir (monkeypatched in conftest).
    client_b = TestClient(create_app(launcher_factory=lambda name: fake_launcher))
    # GET lazily rehydrates the session (and its dispatch_policy) from
    # JSONL into the in-memory store — this is the production read path.
    assert client_b.get(f"/v1/sessions/{session_id}").status_code == 200

    # Trigger MUST be valid because the rehydrated policy is `manual`.
    # In `immediate` mode this would be 409.
    r = client_b.post(f"/v1/sessions/{session_id}/dispatch_policy/trigger")
    assert r.status_code == 202
    assert r.json()["drained"] == 0  # empty queue, but the policy stuck
    client_b.close()


# -- OpenAPI contract (heuristic 5) -------------------------------------------


def test_openapi_documents_the_two_dispatch_policy_routes(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    policy_path = "/v1/sessions/{session_id}/dispatch_policy"
    trigger_path = "/v1/sessions/{session_id}/dispatch_policy/trigger"

    assert policy_path in paths
    assert trigger_path in paths

    # PATCH: discriminated union body is under requestBody. The
    # ``required is True`` check guards the Postman/SDK regression
    # where FastAPI silently emits ``required: false`` for a
    # discriminated union body and Postman shows no body schema at all.
    patch = paths[policy_path]["patch"]
    assert patch["requestBody"]["required"] is True
    body_schema = patch["requestBody"]["content"]["application/json"]["schema"]
    # Discriminated union surfaces as oneOf with a discriminator on ``kind``.
    refs = {item["$ref"].rsplit("/", 1)[-1] for item in body_schema.get("oneOf", [])}
    assert refs == {"ImmediatePolicyRequest", "WorkWindowPolicyRequest", "ManualPolicyRequest"}
    assert body_schema["discriminator"]["propertyName"] == "kind"

    # Each variant resolves and pins its required field set so a future
    # rename (e.g. ``windows`` → ``time_windows``) breaks the test.
    schemas = spec["components"]["schemas"]
    assert "kind" in schemas["ImmediatePolicyRequest"]["required"]
    assert "kind" in schemas["ManualPolicyRequest"]["required"]
    assert sorted(schemas["WorkWindowPolicyRequest"]["required"]) == ["kind", "windows"]

    assert "200" in patch["responses"]

    # POST trigger: response model + 202.
    post = paths[trigger_path]["post"]
    assert "202" in post["responses"]
    trigger_ref = post["responses"]["202"]["content"]["application/json"]["schema"]["$ref"]
    trigger_schema = spec["components"]["schemas"][trigger_ref.rsplit("/", 1)[-1]]
    assert "drained" in trigger_schema["properties"]
    assert trigger_schema["properties"]["drained"]["type"] == "integer"
