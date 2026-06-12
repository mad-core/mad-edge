"""Bounded restart test for startup rehydration (issue #46 Part A + B).

Contract: when Mad restarts with queued work in the JSONL log, the
lifespan rehydrates every session that has pending work into the live
index BEFORE the dispatcher starts, and dispatch resumes ordered by
priority (desc) with arrived_at (asc) as tiebreak — WITHOUT any
per-session GET first. Sessions without pending work are NOT
rehydrated (they stay lazy-loaded).

Boot 1 runs WITHOUT the lifespan (no dispatcher), so enqueued tasks
stay queued in the log. Boot 2 enters the TestClient context manager,
which runs the real lifespan: projection bootstrap → rehydration →
``dispatcher.start()``. The launcher is a ``ScriptedLauncher`` (no real
CLI); every wait is a bounded state poll on the persisted log
(heuristics 7/8).

The seeded layout makes priority order and arrival order point at
DIFFERENT winners: session A's task arrives FIRST and A is created
first, but B carries priority 5 — so a correct resume order can only
come from the real ordering function, not from insertion or arrival.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.adapters.outbound.persistence import jsonl_session_repository as _persistence
from support.launchers import ScriptedLauncher

_DEADLINE_S = 10.0


def _create_session(client: TestClient, session_payload: dict) -> str:
    r = client.post("/v1/sessions", json=session_payload)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _event_types(session_id: str) -> list[str]:
    return [e["type"] for e in _persistence.get_events(session_id)]


def test_restart_rehydrates_pending_sessions_and_resumes_in_priority_order(
    tmp_sessions_dir,
    tmp_workspaces_dir,
    session_payload: dict,
) -> None:
    launcher = ScriptedLauncher()  # unscripted runs self-complete with status_idle

    # -- Boot 1: queue work, set priority, never dispatch ----------------
    client_a = TestClient(create_app(launcher_factory=lambda name: launcher))
    session_low = _create_session(client_a, session_payload)
    r = client_a.post(f"/v1/sessions/{session_low}/tasks", json={"content": "low, arrived first"})
    assert r.status_code == 202, r.text
    low_task_id = r.json()["task_id"]

    session_high = _create_session(client_a, session_payload)
    assert (
        client_a.patch(f"/v1/sessions/{session_high}/priority", json={"priority": 5}).status_code
        == 200
    )
    r = client_a.post(f"/v1/sessions/{session_high}/tasks", json={"content": "high, arrived later"})
    assert r.status_code == 202, r.text
    high_task_id = r.json()["task_id"]

    session_idle = _create_session(client_a, session_payload)  # no tasks — no pending work
    client_a.close()

    # Boot 1 never started the dispatcher: the work is still queued.
    assert "task.dispatched" not in _event_types(session_low)
    assert "task.dispatched" not in _event_types(session_high)

    # -- Boot 2: fresh app, same sessions dir, REAL lifespan -------------
    app_b = create_app(launcher_factory=lambda name: launcher)
    with TestClient(app_b) as client_b:
        # Rehydration happened during startup, before any HTTP request:
        # both pending-work sessions are live, with priority replayed.
        index = app_b.state.store.sessions
        assert set(index) == {session_low, session_high}
        assert session_idle not in index  # negative twin: no pending work
        assert index[session_high].priority == 5
        assert index[session_low].priority == 1

        # Dispatch resumes on its own — poll the persisted log until both
        # tasks complete (bounded; no per-session GET is issued).
        deadline = time.monotonic() + _DEADLINE_S
        while time.monotonic() < deadline:
            if all("task.completed" in _event_types(sid) for sid in (session_low, session_high)):
                break
            time.sleep(0.05)
        for sid, task_id in ((session_low, low_task_id), (session_high, high_task_id)):
            completed = [e for e in _persistence.get_events(sid) if e["type"] == "task.completed"]
            assert [e["task_id"] for e in completed] == [task_id], (
                f"queued work on {sid} did not resume after restart; log types: {_event_types(sid)}"
            )

    # Priority (desc) beats both arrival order and session insertion
    # order: the priority-5 session's task ran first. The launcher's call
    # order IS the dispatch order — single dispatch awaits each run to
    # completion. (Sorting the persisted task.dispatched events by UUIDv7
    # event_id is NOT a valid oracle here: both dispatches can mint
    # within the same millisecond, where lex order is random — the
    # ADR-0005 in-millisecond caveat.) Each task also triggers one
    # post-run auto-sync invocation with an internal prompt (issue #8),
    # filtered out by matching the two real contents.
    task_prompts = [
        c["prompt"]
        for c in launcher.calls
        if c["prompt"] in ("low, arrived first", "high, arrived later")
    ]
    assert task_prompts == ["high, arrived later", "low, arrived first"]
