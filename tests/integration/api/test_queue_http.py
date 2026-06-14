"""HTTP integration tests for ``GET /v1/queue`` (issue #46 Part C).

Contracts under test:

- Three strongly-typed buckets: ``in_flight`` (single task or null),
  ``ready`` (only sessions dispatchable right now, true dispatch order),
  ``scheduled`` (policy-gated sessions with a typed reason).
- Policy groups are never flattened: a work_window-closed high-priority
  session is ABSENT from ``ready`` and PRESENT in ``scheduled``.
- ``ready[0]`` equals the dispatcher's actual pick (Part D — one shared
  ordering function).
- Invariant violations fail loud (hard rule 7), never render silently.

Apps are built without the lifespan context (dispatcher loop never
runs) and with a ``FakeClock`` pinned to 14:00 UTC so window math is
deterministic. Queued/in-flight state is injected directly into
``app.state.task_projection``, the same pattern the dispatch-policy
suite uses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.core.orchestration.domain.task import Task
from support.clock import FakeClock
from support.launchers import ScriptedLauncher

_NOW = datetime(2026, 6, 1, 14, 0, tzinfo=UTC)  # 14:00 UTC — outside 18:00-08:00
_CLOSED_WINDOW = {
    "kind": "work_window",
    "windows": [{"start": "18:00", "end": "08:00", "timezone": "UTC"}],
}


@pytest.fixture
def client(tmp_sessions_dir, tmp_workspaces_dir) -> TestClient:
    launcher = ScriptedLauncher()
    return TestClient(create_app(launcher_factory=lambda name: launcher, clock=FakeClock(_NOW)))


def _create_session(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _inject_queued(
    client: TestClient, session_id: str, *, minute: int, content: str = "queued"
) -> Task:
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
    )
    client.app.state.task_projection._queued.setdefault(session_id, []).append(task)
    return task


def _inject_in_flight(client: TestClient, session_id: str) -> Task:
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content="running",
        scheduled_for="now",
        created_at=datetime(2026, 6, 1, 11, 0, tzinfo=UTC),
    )
    client.app.state.task_projection._in_flight[session_id] = task
    return task


# -- Empty queue -----------------------------------------------------------------


def test_empty_queue_returns_empty_buckets(client: TestClient) -> None:
    r = client.get("/v1/queue")

    assert r.status_code == 200
    assert r.json() == {"in_flight": None, "ready": [], "scheduled": []}


# -- ready ordering ----------------------------------------------------------------


def test_ready_orders_by_priority_desc(client: TestClient, session_payload: dict) -> None:
    """The high-priority session's task leads even though it arrived later
    and its session was created later (insertion order ≠ dispatch order)."""
    low_id = _create_session(client, session_payload)
    high_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{high_id}/priority", json={"priority": 8})
    low_task = _inject_queued(client, low_id, minute=0)
    high_task = _inject_queued(client, high_id, minute=30)

    body = client.get("/v1/queue").json()

    assert [e["task_id"] for e in body["ready"]] == [str(high_task.task_id), str(low_task.task_id)]
    assert body["ready"][0]["session_id"] == high_id
    assert body["ready"][0]["priority"] == 8
    assert body["ready"][1]["priority"] == 1
    assert body["scheduled"] == []


def test_ready_tie_breaks_on_earlier_arrival_not_insertion(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin for insertion-order: the first-created session holds
    the LATER-arrived head, so it must NOT lead the tie."""
    first_created = _create_session(client, session_payload)
    second_created = _create_session(client, session_payload)
    late_task = _inject_queued(client, first_created, minute=45)
    early_task = _inject_queued(client, second_created, minute=5)

    ready = client.get("/v1/queue").json()["ready"]

    assert [e["task_id"] for e in ready] == [str(early_task.task_id), str(late_task.task_id)]


# -- Policy buckets ----------------------------------------------------------------


def test_window_closed_high_priority_session_is_scheduled_not_ready(
    client: TestClient, session_payload: dict
) -> None:
    """The VIP-with-closed-window case the issue forbids flattening: the
    priority-10 session must be ABSENT from ready (its window opens at
    18:00) and the priority-1 immediate session is genuinely next."""
    vip_id = _create_session(client, session_payload)
    plain_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{vip_id}/priority", json={"priority": 10})
    assert (
        client.patch(f"/v1/sessions/{vip_id}/dispatch_policy", json=_CLOSED_WINDOW).status_code
        == 200
    )
    vip_task = _inject_queued(client, vip_id, minute=0)
    plain_task = _inject_queued(client, plain_id, minute=30)

    body = client.get("/v1/queue").json()

    ready_ids = [e["task_id"] for e in body["ready"]]
    assert str(vip_task.task_id) not in ready_ids
    assert ready_ids == [str(plain_task.task_id)]

    assert len(body["scheduled"]) == 1
    gated = body["scheduled"][0]
    assert gated["task_id"] == str(vip_task.task_id)
    assert gated["priority"] == 10
    assert gated["reason"]["kind"] == "window"
    assert datetime.fromisoformat(gated["reason"]["scheduled_for"]) == datetime(
        2026, 6, 1, 18, 0, tzinfo=UTC
    )


def test_manual_session_is_scheduled_with_manual_reason(
    client: TestClient, session_payload: dict
) -> None:
    manual_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{manual_id}/dispatch_policy", json={"kind": "manual"})
    task = _inject_queued(client, manual_id, minute=0)

    body = client.get("/v1/queue").json()

    assert body["ready"] == []
    assert len(body["scheduled"]) == 1
    assert body["scheduled"][0]["task_id"] == str(task.task_id)
    assert body["scheduled"][0]["reason"] == {"kind": "manual", "scheduled_for": None}


def test_scheduled_orders_window_by_time_then_manual_last(
    client: TestClient, session_payload: dict
) -> None:
    """``scheduled`` ordering contract: (scheduled_for asc, then -priority);
    manual entries (no scheduled_for) sort after every dated window."""
    window_low = _create_session(client, session_payload)
    window_high = _create_session(client, session_payload)
    manual_id = _create_session(client, session_payload)
    for sid in (window_low, window_high):
        assert (
            client.patch(f"/v1/sessions/{sid}/dispatch_policy", json=_CLOSED_WINDOW).status_code
            == 200
        )
    client.patch(f"/v1/sessions/{window_high}/priority", json={"priority": 9})
    client.patch(f"/v1/sessions/{manual_id}/dispatch_policy", json={"kind": "manual"})
    client.patch(f"/v1/sessions/{manual_id}/priority", json={"priority": 10})
    low_task = _inject_queued(client, window_low, minute=0)
    high_task = _inject_queued(client, window_high, minute=10)
    manual_task = _inject_queued(client, manual_id, minute=20)

    scheduled = client.get("/v1/queue").json()["scheduled"]

    # Same window ⇒ same scheduled_for ⇒ priority 9 leads; the manual
    # session sorts last despite priority 10 — no date, no slot.
    assert [e["task_id"] for e in scheduled] == [
        str(high_task.task_id),
        str(low_task.task_id),
        str(manual_task.task_id),
    ]


def test_manual_session_with_pending_trigger_is_ready(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin of the manual-gated case: after POST /trigger the
    drain counter authorizes dispatch, so the session belongs in ready."""
    manual_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{manual_id}/dispatch_policy", json={"kind": "manual"})
    task = _inject_queued(client, manual_id, minute=0)
    assert client.post(f"/v1/sessions/{manual_id}/dispatch_policy/trigger").status_code == 202

    body = client.get("/v1/queue").json()

    assert [e["task_id"] for e in body["ready"]] == [str(task.task_id)]
    assert body["scheduled"] == []


# -- Effective policy: deployment-default inheritance (issue #45) --------------------


def test_inheriting_session_is_gated_by_deployment_default_window(
    client: TestClient, session_payload: dict
) -> None:
    """A session with NO per-session override inherits the deployment-wide
    work_window default (issue #45), so the queue view gates it exactly as
    the dispatcher would: scheduled with the window's next opening, never
    ready."""
    assert client.put("/v1/dispatch_policy", json=_CLOSED_WINDOW).status_code == 200
    session_id = _create_session(client, session_payload)
    task = _inject_queued(client, session_id, minute=0)

    body = client.get("/v1/queue").json()

    assert body["ready"] == []
    assert len(body["scheduled"]) == 1
    gated = body["scheduled"][0]
    assert gated["task_id"] == str(task.task_id)
    assert gated["reason"]["kind"] == "window"
    assert datetime.fromisoformat(gated["reason"]["scheduled_for"]) == datetime(
        2026, 6, 1, 18, 0, tzinfo=UTC
    )


def test_pinned_immediate_override_beats_gated_deployment_default(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin: a pinned per-session ``immediate`` override wins over
    the gated deployment default (issue #45 resolution order), so its task
    is ready while the inheriting sibling stays scheduled — and ready[0]
    agrees with the dispatcher's actual pick under that same resolution."""
    assert client.put("/v1/dispatch_policy", json=_CLOSED_WINDOW).status_code == 200
    pinned_id = _create_session(client, session_payload)
    inheriting_id = _create_session(client, session_payload)
    assert (
        client.patch(
            f"/v1/sessions/{pinned_id}/dispatch_policy", json={"kind": "immediate"}
        ).status_code
        == 200
    )
    pinned_task = _inject_queued(client, pinned_id, minute=0)
    inheriting_task = _inject_queued(client, inheriting_id, minute=10)

    body = client.get("/v1/queue").json()
    picked = client.app.state.dispatcher._find_next_dispatchable()

    assert [e["task_id"] for e in body["ready"]] == [str(pinned_task.task_id)]
    assert [e["task_id"] for e in body["scheduled"]] == [str(inheriting_task.task_id)]
    assert picked is not None
    assert str(picked.task_id) == body["ready"][0]["task_id"]


# -- in_flight ----------------------------------------------------------------------


def test_in_flight_task_is_surfaced_with_priority(
    client: TestClient, session_payload: dict
) -> None:
    session_id = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{session_id}/priority", json={"priority": 3})
    task = _inject_in_flight(client, session_id)

    body = client.get("/v1/queue").json()

    assert body["in_flight"] == {
        "task_id": str(task.task_id),
        "session_id": session_id,
        "content": "running",
        "scheduled_for": "now",
        "created_at": body["in_flight"]["created_at"],
        "priority": 3,
    }
    assert datetime.fromisoformat(body["in_flight"]["created_at"]) == task.created_at


# -- Part D: the screen never disagrees with the dispatcher --------------------------


def test_ready_head_equals_dispatcher_actual_pick(
    client: TestClient, session_payload: dict
) -> None:
    """``ready[0]`` and ``Dispatcher._find_next_dispatchable()`` must come
    from the same ordering function — assert they agree on a layout where
    priority order and arrival order point at DIFFERENT tasks."""
    early_low = _create_session(client, session_payload)
    late_high = _create_session(client, session_payload)
    client.patch(f"/v1/sessions/{late_high}/priority", json={"priority": 7})
    _inject_queued(client, early_low, minute=0)
    _inject_queued(client, late_high, minute=50)

    ready = client.get("/v1/queue").json()["ready"]
    picked = client.app.state.dispatcher._find_next_dispatchable()

    assert picked is not None
    assert ready[0]["task_id"] == str(picked.task_id)
    assert picked.session_id == late_high


# -- Invariant violations fail loud (hard rule 7) -------------------------------------


def test_pending_session_missing_from_index_raises(client: TestClient) -> None:
    """A pending task whose session is unknown to the live index means the
    rehydration foundation broke — the view must blow up, not omit work."""
    _inject_queued(client, "sesn_ghost", minute=0)

    with pytest.raises(RuntimeError, match="sesn_ghost"):
        client.get("/v1/queue")


def test_two_in_flight_tasks_violate_single_dispatch_and_raise(
    client: TestClient, session_payload: dict
) -> None:
    a = _create_session(client, session_payload)
    b = _create_session(client, session_payload)
    _inject_in_flight(client, a)
    _inject_in_flight(client, b)

    with pytest.raises(RuntimeError, match="single-dispatch"):
        client.get("/v1/queue")


# -- OpenAPI contract (heuristic 5) ----------------------------------------------------


def test_openapi_documents_the_queue_route(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    get = spec["paths"]["/v1/queue"]["get"]
    response_ref = get["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    schemas = spec["components"]["schemas"]
    queue_schema = schemas[response_ref.rsplit("/", 1)[-1]]

    assert sorted(queue_schema["required"]) == ["ready", "scheduled"]
    ready_ref = queue_schema["properties"]["ready"]["items"]["$ref"].rsplit("/", 1)[-1]
    entry = schemas[ready_ref]
    assert sorted(entry["required"]) == [
        "content",
        "created_at",
        "priority",
        "scheduled_for",
        "session_id",
        "task_id",
    ]
    scheduled_ref = queue_schema["properties"]["scheduled"]["items"]["$ref"].rsplit("/", 1)[-1]
    scheduled_entry = schemas[scheduled_ref]
    assert "reason" in scheduled_entry["required"]
    reason_ref = scheduled_entry["properties"]["reason"]["$ref"].rsplit("/", 1)[-1]
    assert schemas[reason_ref]["properties"]["kind"]["enum"] == ["window", "manual"]
