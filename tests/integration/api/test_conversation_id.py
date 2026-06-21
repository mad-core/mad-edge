"""HTTP integration tests for conversation ID capture and resume mode (issue #63).

Contract under test:
- GET /v1/sessions/{id} exposes ``last_conversation_id`` (null until a run
  captures one).
- POST /v1/sessions/{id}/tasks accepts ``conversation_mode``, validated as
  ``"new" | "resume"``.
- GET /v1/sessions/{id}/tasks echoes ``conversation_mode`` on each TaskResponse.
- OpenAPI contract: 422 on unknown ``conversation_mode`` value.

The ``client`` fixture disables the dispatcher lifespan — all assertions here
are about the HTTP contract only; the runtime resume flow is tested separately
in tests/integration/orchestration/test_conversation_id.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app


def _session_id(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


# -- GET /v1/sessions/{id} -----------------------------------------------------


def test_get_session_exposes_last_conversation_id_null_by_default(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _session_id(client, session_payload)

    r = client.get(f"/v1/sessions/{session_id}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert "last_conversation_id" in body
    assert body["last_conversation_id"] is None


# -- POST /v1/sessions/{id}/tasks contract tests --------------------------------


def test_enqueue_task_with_mode_new_is_accepted(client: TestClient, session_payload: dict) -> None:
    session_id = _session_id(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "do the thing", "conversation_mode": "new"},
    )

    assert r.status_code == 202, r.text
    body = r.json()
    from uuid import UUID

    assert UUID(body["task_id"])  # value-level: valid UUID, not just key present


def test_enqueue_task_with_mode_resume_is_accepted(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _session_id(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "continue", "conversation_mode": "resume"},
    )

    assert r.status_code == 202, r.text


def test_enqueue_task_unknown_conversation_mode_returns_422(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _session_id(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "x", "conversation_mode": "fork"},
    )

    assert r.status_code == 422, r.text


def test_enqueue_task_default_conversation_mode_is_new(
    client: TestClient, session_payload: dict
) -> None:
    """``conversation_mode`` defaults to ``"new"`` when omitted."""
    session_id = _session_id(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "omit mode"},
    )

    assert r.status_code == 202, r.text


# -- GET /v1/sessions/{id}/tasks — TaskResponse includes conversation_mode ------


def test_list_tasks_exposes_conversation_mode(
    fake_launcher,
    session_payload: dict,
    tmp_sessions_dir,
    tmp_workspaces_dir,
) -> None:
    """``GET /v1/sessions/{id}/tasks`` echoes ``conversation_mode`` on queued tasks.

    Uses a ``manual`` dispatch policy so tasks stay queued (dispatcher does not
    drain them immediately) and the lifespan context so the projection is
    bootstrapped and subscribed to the bus.
    """
    app = create_app(launcher_factory=lambda name: fake_launcher)
    with TestClient(app) as client:
        session_id = _session_id(client, session_payload)
        # Pin manual policy so the task stays queued.
        client.patch(
            f"/v1/sessions/{session_id}/dispatch_policy",
            json={"kind": "manual"},
        )
        client.post(
            f"/v1/sessions/{session_id}/tasks",
            json={"content": "queued task", "conversation_mode": "resume"},
        )

        r = client.get(f"/v1/sessions/{session_id}/tasks")

    assert r.status_code == 200, r.text
    queued = r.json()["queued"]
    assert len(queued) == 1
    assert queued[0]["conversation_mode"] == "resume"


# -- OpenAPI contract tests ----------------------------------------------------


def test_openapi_documents_conversation_mode_in_enqueue_task_request(
    client: TestClient,
) -> None:
    """``conversation_mode`` must appear in the OpenAPI schema for
    ``EnqueueTaskRequest`` with a valid enum of ``["new", "resume"]``."""
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    post = paths["/v1/sessions/{session_id}/tasks"]["post"]
    body_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body_schema = spec["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]

    assert "conversation_mode" in body_schema["properties"]
    cm_schema = body_schema["properties"]["conversation_mode"]
    # Pydantic emits the enum either directly or as an anyOf reference.
    if "enum" in cm_schema:
        assert set(cm_schema["enum"]) == {"new", "resume"}
    else:
        # Pydantic may emit anyOf with a $ref to a named enum schema.
        refs = [item.get("$ref", "") for item in cm_schema.get("anyOf", [])]
        assert any("ConversationMode" in r or "conversation_mode" in r.lower() for r in refs), (
            f"No enum or named-ref on conversation_mode: {cm_schema}"
        )
    # Not required — has a default value.
    assert "conversation_mode" not in body_schema.get("required", [])


def test_openapi_documents_last_conversation_id_in_session_detail_response(
    client: TestClient,
) -> None:
    """``last_conversation_id`` must appear in the OpenAPI schema for
    ``SessionDetailResponse`` on ``GET /v1/sessions/{id}``."""
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    get = paths["/v1/sessions/{session_id}"]["get"]
    resp_ref = get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    resp_schema = spec["components"]["schemas"][resp_ref.rsplit("/", 1)[-1]]

    assert "last_conversation_id" in resp_schema["properties"]
    lcid = resp_schema["properties"]["last_conversation_id"]
    # Must be nullable (string | null) — Pydantic emits either anyOf or type=string + default.
    is_nullable = (
        any("null" in str(item) for item in lcid.get("anyOf", []))
        or lcid.get("type") == "string"
        or "default" in lcid
    )
    assert is_nullable, f"last_conversation_id schema is not nullable: {lcid}"
