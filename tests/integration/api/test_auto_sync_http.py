"""HTTP integration tests for the post-run auto-sync gate (issue #109).

Mad used to fire a second, unrequested "auto-sync" launcher run after EVERY
primary run. That run publishes leftover work to a hard-coded ``mad/<session_id>``
branch and opens a PR — so a task already managing its own named branch/PR got a
**duplicate** PR next to the real one. The fix is a deterministic gate resolved in
Mad (task > session > ``MAD_AUTO_SYNC`` > ``False`` — off by default, opt-in),
not an instruction we hope the agent obeys.

The observable contract at this boundary:

* ``POST /v1/sessions`` accepts ``auto_sync`` and persists it on
  ``session.created``, so it survives a rebuild from the log (hard rule 6).
* With the gate OFF (the default, or an explicit ``false``) the launcher is
  invoked **exactly once** (the primary run) — the second run never starts, so no
  branch and no PR can be created — and a non-terminal ``agent.autosync.skipped``
  records the decision for operators.
* With the gate ON (an explicit ``true``) the post-run publish fires: **exactly
  twice**.
* Both JSON bodies declare the field in OpenAPI as an optional nullable boolean,
  and a non-boolean is rejected at the boundary with 422 (hard rule 9).

All waits poll the launcher call log / the session JSONL on a state predicate
with a deadline (rules 7 and 8) — never a bare sleep-then-count.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from mad.core.orchestration.domain.task import Task
from mad.core.sessions.domain.rehydrate import rehydrate_from_events
from support.launchers import ScriptedLauncher

_DEADLINE_S = 5.0


# -- Helpers -------------------------------------------------------------------


def _session_body(bare_repo: Path, **extra: object) -> dict:
    body: dict = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    body.update(extra)
    return body


def _create_session(client: TestClient, body: dict) -> str:
    r = client.post("/v1/sessions", json=body)
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _script_two_runs(launcher: ScriptedLauncher) -> None:
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )


def _read_log(sessions_dir: Path, session_id: str) -> list[dict]:
    log_path = sessions_dir / f"{session_id}.jsonl"
    if not log_path.exists():
        return []
    return [json.loads(ln) for ln in log_path.read_text().splitlines() if ln.strip()]


def _wait_for_logged_event(sessions_dir: Path, session_id: str, event_type: str) -> list[dict]:
    """Poll the session log until ``event_type`` is appended; fail with what we saw.

    Bounded by ``_DEADLINE_S`` (rule 8). ``agent.autosync.skipped`` is the last
    event ``_run_launcher`` emits on the gated path, so its presence means the
    background run has completed and any launcher call count read afterwards is
    final, not a mid-flight snapshot.
    """
    deadline = time.monotonic() + _DEADLINE_S
    lines: list[dict] = []
    while time.monotonic() < deadline:
        lines = _read_log(sessions_dir, session_id)
        if any(e.get("type") == event_type for e in lines):
            return lines
        time.sleep(0.02)
    pytest.fail(
        f"timed out waiting for {event_type!r} in the session log; "
        f"got types={[e.get('type') for e in lines]}"
    )


def _wait_for_launcher_calls(launcher: ScriptedLauncher, count: int) -> None:
    deadline = time.monotonic() + _DEADLINE_S
    while time.monotonic() < deadline:
        if len(launcher.calls) >= count:
            return
        time.sleep(0.02)
    pytest.fail(
        f"timed out waiting for {count} launcher runs; got {[c['prompt'] for c in launcher.calls]}"
    )


# -- The gate: auto_sync=false suppresses the second launcher run ---------------


def test_auto_sync_false_invokes_the_launcher_exactly_once(
    client: TestClient,
    fake_launcher: ScriptedLauncher,
    bare_repo: Path,
    tmp_sessions_dir: Path,
) -> None:
    """A session created with ``auto_sync: false`` runs the primary prompt and
    NOTHING else — the post-run publish that opened the duplicate PR never fires."""
    _script_two_runs(fake_launcher)  # a regression would happily consume the 2nd
    session_id = _create_session(client, _session_body(bare_repo, auto_sync=False))

    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    assert r.status_code == 200, r.text

    lines = _wait_for_logged_event(tmp_sessions_dir, session_id, "agent.autosync.skipped")

    assert len(fake_launcher.calls) == 1, (
        "auto_sync=false must suppress the post-run auto-sync run; launcher prompts "
        f"were {[c['prompt'] for c in fake_launcher.calls]}"
    )
    assert fake_launcher.calls[0]["prompt"] == "do work"

    skipped = next(e for e in lines if e["type"] == "agent.autosync.skipped")
    assert skipped["reason"] == "disabled"


def test_auto_sync_default_invokes_the_launcher_exactly_once(
    client: TestClient,
    fake_launcher: ScriptedLauncher,
    bare_repo: Path,
    tmp_sessions_dir: Path,
) -> None:
    """With ``auto_sync`` omitted the gate is OFF by default (issue #109) — the
    launcher runs exactly once and the skip is recorded, so no duplicate PR. Twin
    of ``test_auto_sync_true_invokes_the_launcher_twice``."""
    _script_two_runs(fake_launcher)  # a regression would happily consume the 2nd
    session_id = _create_session(client, _session_body(bare_repo))

    client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    lines = _wait_for_logged_event(tmp_sessions_dir, session_id, "agent.autosync.skipped")

    assert len(fake_launcher.calls) == 1, (
        "auto_sync omitted must default OFF and suppress the post-run run; launcher "
        f"prompts were {[c['prompt'] for c in fake_launcher.calls]}"
    )
    assert fake_launcher.calls[0]["prompt"] == "do work"
    skipped = next(e for e in lines if e["type"] == "agent.autosync.skipped")
    assert skipped["reason"] == "disabled"


def test_auto_sync_true_invokes_the_launcher_twice(
    client: TestClient,
    fake_launcher: ScriptedLauncher,
    bare_repo: Path,
    tmp_sessions_dir: Path,
) -> None:
    """An explicit ``auto_sync: true`` keeps the post-run publish — the field is a
    real toggle, not a one-way "any value disables it" switch."""
    _script_two_runs(fake_launcher)
    session_id = _create_session(client, _session_body(bare_repo, auto_sync=True))

    client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    _wait_for_launcher_calls(fake_launcher, 2)

    assert len(fake_launcher.calls) == 2
    assert "agent.autosync.skipped" not in [
        e["type"] for e in _read_log(tmp_sessions_dir, session_id)
    ]


def test_env_auto_sync_false_invokes_the_launcher_exactly_once(
    client: TestClient,
    fake_launcher: ScriptedLauncher,
    bare_repo: Path,
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The operator can disable auto-sync deployment-wide with ``MAD_AUTO_SYNC``;
    a session that omits the field inherits it."""
    monkeypatch.setenv("MAD_AUTO_SYNC", "false")
    _script_two_runs(fake_launcher)
    session_id = _create_session(client, _session_body(bare_repo))

    client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    _wait_for_logged_event(tmp_sessions_dir, session_id, "agent.autosync.skipped")

    assert len(fake_launcher.calls) == 1


def test_session_auto_sync_true_overrides_env_false(
    client: TestClient,
    fake_launcher: ScriptedLauncher,
    bare_repo: Path,
    tmp_sessions_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative twin of the env case: the per-session value is more specific than
    ``MAD_AUTO_SYNC`` and wins."""
    monkeypatch.setenv("MAD_AUTO_SYNC", "false")
    _script_two_runs(fake_launcher)
    session_id = _create_session(client, _session_body(bare_repo, auto_sync=True))

    client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    _wait_for_launcher_calls(fake_launcher, 2)

    assert len(fake_launcher.calls) == 2
    assert "agent.autosync.skipped" not in [
        e["type"] for e in _read_log(tmp_sessions_dir, session_id)
    ]


# -- Round-trip: the value survives a rebuild from the log (hard rule 6) --------


def test_auto_sync_false_survives_rehydration_from_the_session_log(
    client: TestClient, bare_repo: Path, tmp_sessions_dir: Path
) -> None:
    """``session.created`` carries ``auto_sync``, so a session rebuilt from its
    JSONL after a crash comes back opted OUT — if it did not, the next idle would
    re-open the duplicate PR this issue fixes."""
    session_id = _create_session(client, _session_body(bare_repo, auto_sync=False))

    lines = _read_log(tmp_sessions_dir, session_id)
    created = next(e for e in lines if e["type"] == "session.created")
    assert created["auto_sync"] is False

    rebuilt = rehydrate_from_events(session_id, lines)
    assert rebuilt.auto_sync is False


def test_auto_sync_omitted_rehydrates_as_none_meaning_inherit(
    client: TestClient, bare_repo: Path, tmp_sessions_dir: Path
) -> None:
    """Negative twin: an omitted field persists and rebuilds as ``None`` — "no
    per-session override", NOT ``False``. Only an explicit value opts out."""
    session_id = _create_session(client, _session_body(bare_repo))

    lines = _read_log(tmp_sessions_dir, session_id)
    created = next(e for e in lines if e["type"] == "session.created")
    assert created["auto_sync"] is None

    rebuilt = rehydrate_from_events(session_id, lines)
    assert rebuilt.auto_sync is None


# -- Typed boundary: 422 on a non-boolean (hard rule 9) ------------------------


def test_create_session_rejects_non_boolean_auto_sync_422(
    client: TestClient, bare_repo: Path
) -> None:
    """Negative twin of the happy path: a junk value is rejected at the boundary,
    never coerced. ``"maybe"`` must not read as truthy and silently keep the
    duplicate-PR behaviour the caller was trying to turn off."""
    r = client.post("/v1/sessions", json=_session_body(bare_repo, auto_sync="maybe"))

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert any(d.get("loc") == ["body", "auto_sync"] for d in detail), detail


def test_enqueue_task_rejects_non_boolean_auto_sync_422(
    client: TestClient, session_payload: dict
) -> None:
    """Same guard on the task body."""
    session_id = _create_session(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "do the thing", "auto_sync": "maybe"},
    )

    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert any(d.get("loc") == ["body", "auto_sync"] for d in detail), detail


def test_enqueue_task_accepts_auto_sync_false_202(
    client: TestClient, session_payload: dict
) -> None:
    """Happy path for the task body: ``auto_sync: false`` is accepted and queued."""
    session_id = _create_session(client, session_payload)

    r = client.post(
        f"/v1/sessions/{session_id}/tasks",
        json={"content": "do the thing", "auto_sync": False},
    )

    assert r.status_code == 202, r.text
    assert r.json()["status"] == "queued"


# -- TaskResponse exposes the resolved per-task override -----------------------


def _inject_queued(client: TestClient, session_id: str, *, auto_sync: bool | None) -> None:
    """Place a Task on the in-process projection — the same surface the dispatcher
    updates via ``apply()``, minus the bus round-trip (the conftest ``client`` runs
    no lifespan, so the dispatcher stays quiescent and these stay deterministic)."""
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content="queued work",
        scheduled_for="now",
        created_at=datetime(2026, 7, 1, tzinfo=UTC),
        auto_sync=auto_sync,
    )
    client.app.state.task_projection._queued.setdefault(session_id, []).append(task)


def test_list_tasks_exposes_auto_sync_false(client: TestClient, session_payload: dict) -> None:
    """``GET /v1/sessions/{id}/tasks`` surfaces the per-task override so a caller
    can see which queued jobs will skip the publish step."""
    session_id = _create_session(client, session_payload)
    _inject_queued(client, session_id, auto_sync=False)

    r = client.get(f"/v1/sessions/{session_id}/tasks")

    assert r.status_code == 200, r.text
    queued = r.json()["queued"]
    assert len(queued) == 1
    assert queued[0]["auto_sync"] is False


def test_list_tasks_reports_null_auto_sync_when_task_has_no_override(
    client: TestClient, session_payload: dict
) -> None:
    """Negative twin: no override serialises as ``null`` (inherit), not ``false``."""
    session_id = _create_session(client, session_payload)
    _inject_queued(client, session_id, auto_sync=None)

    r = client.get(f"/v1/sessions/{session_id}/tasks")

    assert r.status_code == 200, r.text
    queued = r.json()["queued"]
    assert len(queued) == 1
    assert queued[0]["auto_sync"] is None


# -- OpenAPI contract (rule 5) -------------------------------------------------


def _resolve_ref(spec: dict, ref: str) -> dict:
    return spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]


def _request_body_schema(spec: dict, path: str) -> dict:
    body = spec["paths"][path]["post"]["requestBody"]
    assert body["required"] is True, f"{path} POST must declare a required JSON body"
    return _resolve_ref(spec, body["content"]["application/json"]["schema"]["$ref"])


def test_openapi_create_session_declares_optional_boolean_auto_sync(client: TestClient) -> None:
    """CreateSessionRequest exposes ``auto_sync`` as an optional nullable boolean —
    this is what populates /docs and Postman (hard rule 9)."""
    spec = client.get("/openapi.json").json()
    component = _request_body_schema(spec, "/v1/sessions")

    props = component["properties"]
    assert "auto_sync" in props, f"CreateSessionRequest is missing auto_sync: {sorted(props)}"
    # Optional: a client that never heard of #109 keeps working (and keeps the
    # safety net), so the field must not be in `required`.
    assert "auto_sync" not in set(component.get("required", []))
    types = {entry.get("type") for entry in props["auto_sync"]["anyOf"]}
    assert types == {"boolean", "null"}, f"unexpected types for auto_sync: {types}"


def test_openapi_enqueue_task_declares_optional_boolean_auto_sync(client: TestClient) -> None:
    """EnqueueTaskRequest exposes the same field — the per-task level is the one
    that lets a single job opt out without the whole session doing so."""
    spec = client.get("/openapi.json").json()
    component = _request_body_schema(spec, "/v1/sessions/{session_id}/tasks")

    props = component["properties"]
    assert "auto_sync" in props, f"EnqueueTaskRequest is missing auto_sync: {sorted(props)}"
    assert "auto_sync" not in set(component.get("required", []))
    assert component["required"] == ["content"], (
        "content stays the only required task field; auto_sync must be opt-in"
    )
    types = {entry.get("type") for entry in props["auto_sync"]["anyOf"]}
    assert types == {"boolean", "null"}, f"unexpected types for auto_sync: {types}"


def test_openapi_task_response_declares_auto_sync(client: TestClient) -> None:
    """The read model declares it too, so a client can introspect what a queued
    task will do without replaying the event log."""
    spec = client.get("/openapi.json").json()
    get_op = spec["paths"]["/v1/sessions/{session_id}/tasks"]["get"]
    list_schema = _resolve_ref(
        spec, get_op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    )
    task_schema = _resolve_ref(spec, list_schema["properties"]["queued"]["items"]["$ref"])

    props = task_schema["properties"]
    assert "auto_sync" in props, f"TaskResponse is missing auto_sync: {sorted(props)}"
    types = {entry.get("type") for entry in props["auto_sync"]["anyOf"]}
    assert types == {"boolean", "null"}
