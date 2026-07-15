"""Integration tests for issue #61: agent-agnostic timeout with per-session override.

Covers the ``timeout_s`` request field on ``POST /v1/sessions``, the
resolution order (per-session > ``MAD_AGENT_TIMEOUT_S`` env > 600 s default)
threaded all the way to ``AgentLauncher.run(timeout_s=...)``, the 422 guard
for non-positive values, and the OpenAPI contract.

Every positive case has a matching negative twin (CLAUDE.md hard rule 10).
The launcher's resolved timeout is observed via ``ScriptedLauncher.calls``,
which records the ``timeout_s`` keyword each ``run`` receives.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from support.launchers import ScriptedLauncher


def _create_session(client: TestClient, payload: dict) -> dict:
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _send_and_wait(client: TestClient, fake_launcher: ScriptedLauncher, session_id: str) -> None:
    fake_launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": "do work"})
    assert r.status_code == 200, r.text
    deadline = time.monotonic() + 5.0
    while len(fake_launcher.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(fake_launcher.calls) == 2, (
        f"expected 2 launcher invocations (primary + auto-sync), got {len(fake_launcher.calls)}"
    )


def _base_payload(timeout_s: float | None = None) -> dict:
    # Auto-sync is off by default (issue #109). These tests verify the resolved
    # timeout threads to BOTH the primary run and the post-run auto-sync run
    # (``_send_and_wait`` waits for two launcher calls and
    # ``test_per_session_timeout_threads_to_launcher`` asserts on ``calls[1]``),
    # so every session here opts in to auto-sync explicitly.
    payload: dict = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "auto_sync": True,
        "resources": [{"type": "file", "content": "x\n", "mount_path": "/workspace/in.txt"}],
    }
    if timeout_s is not None:
        payload["timeout_s"] = timeout_s
    return payload


# ---------------------------------------------------------------------------
# Resolution order: per-session > env > 600 s default — threaded to the launcher
# ---------------------------------------------------------------------------


def test_per_session_timeout_threads_to_launcher(
    client: TestClient, fake_launcher: ScriptedLauncher
) -> None:
    """A per-session timeout_s reaches AgentLauncher.run as the resolved budget."""
    created = _create_session(client, _base_payload(timeout_s=42.0))
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["timeout_s"] == 42.0
    # The post-run auto-sync run inherits the same resolved timeout.
    assert fake_launcher.calls[1]["timeout_s"] == 42.0


def test_no_override_falls_back_to_hardcoded_default(
    client: TestClient, fake_launcher: ScriptedLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: no timeout_s and no env var → the 600 s default threads through."""
    monkeypatch.delenv("MAD_AGENT_TIMEOUT_S", raising=False)
    created = _create_session(client, _base_payload())
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["timeout_s"] == 600.0


def test_env_default_used_when_no_per_session_override(
    client: TestClient, fake_launcher: ScriptedLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MAD_AGENT_TIMEOUT_S supplies the budget when the request omits timeout_s."""
    monkeypatch.setenv("MAD_AGENT_TIMEOUT_S", "123")
    created = _create_session(client, _base_payload())
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["timeout_s"] == 123.0


def test_per_session_timeout_wins_over_env(
    client: TestClient, fake_launcher: ScriptedLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Precedence: a per-session timeout_s overrides MAD_AGENT_TIMEOUT_S."""
    monkeypatch.setenv("MAD_AGENT_TIMEOUT_S", "123")
    created = _create_session(client, _base_payload(timeout_s=7.0))
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["timeout_s"] == 7.0


# ---------------------------------------------------------------------------
# 422 validation: timeout_s must be > 0
# ---------------------------------------------------------------------------


def test_zero_timeout_is_rejected_422(client: TestClient) -> None:
    """A non-positive timeout_s is rejected at the boundary (hard rule 9)."""
    r = client.post("/v1/sessions", json=_base_payload(timeout_s=0))
    assert r.status_code == 422, r.text


def test_negative_timeout_is_rejected_422(client: TestClient) -> None:
    """Negative twin: a negative timeout_s is likewise rejected."""
    r = client.post("/v1/sessions", json=_base_payload(timeout_s=-5))
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# OpenAPI contract — heuristic 5
# ---------------------------------------------------------------------------


def _resolve_ref(spec: dict, ref: str) -> dict:
    name = ref.rsplit("/", 1)[-1]
    return spec["components"]["schemas"][name]


def test_openapi_create_session_declares_timeout_s_field(client: TestClient) -> None:
    """timeout_s must appear in the OpenAPI schema for CreateSessionRequest."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions"]["post"]
    schema_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    props = component["properties"]
    assert "timeout_s" in props, (
        f"CreateSessionRequest is missing timeout_s; declared: {sorted(props)}"
    )
    # The field is optional — must NOT appear in required.
    required = set(component.get("required", []))
    assert "timeout_s" not in required


def test_openapi_create_session_timeout_s_is_optional_number(client: TestClient) -> None:
    """Type contract: timeout_s is a nullable number (float | None = None)."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions"]["post"]
    schema_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    ts = component["properties"]["timeout_s"]
    any_of = ts.get("anyOf")
    assert any_of is not None, f"expected anyOf for nullable number, got {ts!r}"
    types = {entry.get("type") for entry in any_of}
    assert types == {"number", "null"}, f"unexpected types: {types}"
