"""HTTP integration tests for the orchestration routes (issue #28).

Endpoints under test (per ADR-0009):
- POST   /v1/sessions/{session_id}/tasks
- GET    /v1/sessions/{session_id}/tasks
- DELETE /v1/sessions/{session_id}/tasks/{task_id}
- POST   /v1/sessions/{session_id}/messages → 409 when a queued task is in flight

The conftest ``client`` fixture creates a TestClient *without* a ``with``
block, so the FastAPI lifespan never runs and the dispatcher stays
quiescent. That keeps these tests deterministic — we don't race the
dispatch loop. Tests that need queued/in-flight state inject it
directly into ``app.state.task_projection``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from mad.core.orchestration.domain.task import Task

# -- Helpers -------------------------------------------------------------------


def _create_session(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _inject_queued(client: TestClient, session_id: str, *, content: str = "queued work") -> Task:
    """Place a Task directly on the projection's queued list.

    The projection adapter is in-process (in-memory), so this is the
    same surface the dispatcher would update via ``apply()`` — we just
    skip the bus round-trip.
    """
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


def _inject_in_flight(
    client: TestClient, session_id: str, *, content: str = "running work"
) -> Task:
    projection = client.app.state.task_projection
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    projection._in_flight[session_id] = task
    return task


# -- POST /v1/sessions/{id}/tasks ---------------------------------------------


def test_post_tasks_enqueues_and_returns_202(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)

    r = client.post(f"/v1/sessions/{session_id}/tasks", json={"content": "do the thing"})

    assert r.status_code == 202, r.text
    body = r.json()
    assert UUID(body["task_id"])  # parses as a real UUID
    assert body["session_id"] == session_id
    assert body["scheduled_for"] == "now"
    assert body["status"] == "queued"


def test_post_tasks_passes_through_explicit_scheduled_for(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "overnight work", "scheduled_for": "next_window"},
    )

    assert r.status_code == 202
    assert r.json()["scheduled_for"] == "next_window"


def test_post_tasks_unknown_session_returns_404(client: TestClient) -> None:
    r = client.post("/v1/sessions/sesn_missing/tasks", json={"content": "anything"})
    assert r.status_code == 404
    assert "sesn_missing" in r.json()["detail"]


def test_post_tasks_missing_content_returns_422(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)
    r = client.post(f"/v1/sessions/{session_id}/tasks", json={})
    assert r.status_code == 422
    # Pydantic surfaces "field required" / "missing" — accept either form.
    detail = r.json()["detail"]
    assert any(d.get("loc", []) == ["body", "content"] for d in detail)


# -- GET /v1/sessions/{id}/tasks ----------------------------------------------


def test_get_tasks_returns_empty_lists_for_a_fresh_session(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    r = client.get(f"/v1/sessions/{session_id}/tasks")
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] == []
    assert body["in_flight"] is None


def test_get_tasks_returns_queued_and_in_flight_when_set(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    in_flight = _inject_in_flight(client, session_id, content="running")
    queued_a = _inject_queued(client, session_id, content="next_a")
    queued_b = _inject_queued(client, session_id, content="next_b")

    r = client.get(f"/v1/sessions/{session_id}/tasks")
    assert r.status_code == 200
    body = r.json()

    assert body["in_flight"]["task_id"] == str(in_flight.task_id)
    assert body["in_flight"]["content"] == "running"
    assert [t["task_id"] for t in body["queued"]] == [
        str(queued_a.task_id),
        str(queued_b.task_id),
    ]


def test_get_tasks_unknown_session_returns_404(client: TestClient) -> None:
    r = client.get("/v1/sessions/sesn_missing/tasks")
    assert r.status_code == 404


# -- DELETE /v1/sessions/{id}/tasks/{task_id} ---------------------------------


def test_delete_tasks_cancels_a_queued_task(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)
    task = _inject_queued(client, session_id, content="will be cancelled")

    r = client.delete(f"/v1/sessions/{session_id}/tasks/{task.task_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["task_id"] == str(task.task_id)


def test_delete_tasks_unknown_session_returns_404(client: TestClient) -> None:
    r = client.delete(f"/v1/sessions/sesn_missing/tasks/{uuid4()}")
    assert r.status_code == 404


def test_delete_tasks_unknown_task_returns_404(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)
    r = client.delete(f"/v1/sessions/{session_id}/tasks/{uuid4()}")
    assert r.status_code == 404


def test_delete_tasks_in_flight_returns_409(client: TestClient, session_payload: dict) -> None:
    session_id = _create_session(client, session_payload)
    task = _inject_in_flight(client, session_id, content="cannot cancel running")

    r = client.delete(f"/v1/sessions/{session_id}/tasks/{task.task_id}")
    assert r.status_code == 409
    assert "already dispatched" in r.json()["detail"]


# -- POST /v1/sessions/{id}/messages ↔ queued task in flight (ADR-0009 #6) ----


def test_post_messages_returns_409_when_queued_task_is_in_flight(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    task = _inject_in_flight(client, session_id, content="dispatcher's task")

    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": "interrupt"})

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert str(task.task_id) in detail
    assert "DELETE /tasks/" in detail


def test_post_messages_succeeds_when_no_queued_task_in_flight(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    # No in-flight task injected — /messages should accept the request.
    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": "go"})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"


# -- OpenAPI contract (heuristic 5) -------------------------------------------


def test_openapi_documents_the_three_orchestration_routes(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]

    tasks_path = "/v1/sessions/{session_id}/tasks"
    task_id_path = "/v1/sessions/{session_id}/tasks/{task_id}"

    assert tasks_path in paths, sorted(paths.keys())
    assert task_id_path in paths

    # POST: typed body + 202 status code from response model.
    post = paths[tasks_path]["post"]
    body_ref = post["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    body_schema = spec["components"]["schemas"][body_ref.rsplit("/", 1)[-1]]
    assert body_schema["properties"]["content"]["type"] == "string"
    assert body_schema["properties"]["scheduled_for"]["type"] == "string"
    assert body_schema["required"] == ["content"]
    assert "202" in post["responses"]

    # GET: typed response model.
    get_ref = paths[tasks_path]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    list_schema = spec["components"]["schemas"][get_ref.rsplit("/", 1)[-1]]
    assert "queued" in list_schema["properties"]
    assert "in_flight" in list_schema["properties"]

    # DELETE: declared response model.
    delete = paths[task_id_path]["delete"]
    assert "200" in delete["responses"]
