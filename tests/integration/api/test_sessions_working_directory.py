"""Integration tests for issue #40: launch agents in the cloned repo, not the workspace root.

Covers the working_directory request field, the auto-derivation rule for the
single github_repository case, the path-traversal guard, and the rehydration
fallback for legacy session logs that predate the field.

Every positive case has a matching negative twin (CLAUDE.md hard rule 10).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mad.core.sessions.domain.rehydrate import rehydrate_from_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bare_repo(tmp_path: Path, name: str = "origin.git") -> Path:
    seed = tmp_path / f"seed_{name}"
    seed.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    (seed / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(seed), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(seed),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    bare = tmp_path / name
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
    return bare


def _create_session(client: TestClient, payload: dict) -> dict:
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def _send_and_wait(
    client: TestClient, fake_launcher, session_id: str, content: str = "do work"
) -> None:
    # Auto-sync is off by default (issue #109), so a message drives exactly one
    # launcher run (the primary). These tests assert only on the primary run's
    # working directory (``calls[0]``); the post-run auto-sync run is unrelated to
    # the cwd contract under test, so the one-call default is the faithful reality.
    #
    # Wait on the SETTLED terminal signal, not on ``len(calls) < 1``: the gate
    # emits ``agent.autosync.skipped`` after ``_run_launcher`` has passed the
    # publish decision, so its presence proves no second run will follow. Polling
    # the call count alone would assert ``== 1`` the instant the primary lands and
    # could false-green if a regression re-enabled auto-sync-by-default (the second
    # run would arrive after the assert). If auto-sync ever regresses on, the skip
    # event never appears and this times out on the explicit presence assertion.
    fake_launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])
    r = client.post(f"/v1/sessions/{session_id}/messages", json={"content": content})
    assert r.status_code == 200, r.text
    deadline = time.monotonic() + 5.0
    skipped = False
    while time.monotonic() < deadline:
        detail = client.get(f"/v1/sessions/{session_id}")
        assert detail.status_code == 200, detail.text
        if any(e.get("type") == "agent.autosync.skipped" for e in detail.json()["events"]):
            skipped = True
            break
        time.sleep(0.05)
    assert skipped, (
        "expected an 'agent.autosync.skipped' event (auto-sync off by default) — "
        "its absence means auto-sync ran, i.e. the default regressed to on"
    )
    assert len(fake_launcher.calls) == 1, (
        f"expected exactly one launcher invocation (auto-sync off by default), "
        f"got {len(fake_launcher.calls)}"
    )


# ---------------------------------------------------------------------------
# Positive: single github mount → launcher cwd = repo path (auto-derive)
# ---------------------------------------------------------------------------


def test_single_github_mount_auto_derives_cwd_to_repo(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """No working_directory in the request → cwd auto-derives from the only github mount."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    created = _create_session(client, payload)
    expected_cwd = Path(created["resources_mounted"][0]["local_path"])
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["workspace"] == expected_cwd, (
        f"launcher cwd must be the cloned repo path; "
        f"got {fake_launcher.calls[0]['workspace']!r} expected {expected_cwd!r}"
    )


def test_explicit_working_directory_matching_mount_path_resolves_to_repo(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """Explicit working_directory pointing at the same mount path → same result."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "working_directory": "/workspace/repo",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    created = _create_session(client, payload)
    expected_cwd = Path(created["resources_mounted"][0]["local_path"])
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["workspace"] == expected_cwd


def test_explicit_working_directory_points_to_unmounted_subpath(
    client: TestClient, fake_launcher, bare_repo: Path
) -> None:
    """Explicit working_directory can point to any valid /workspace subpath, not just a mount."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "working_directory": "/workspace/elsewhere",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    created = _create_session(client, payload)
    workspace_root = Path(created["workspace"])
    expected_cwd = workspace_root / "elsewhere"
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["workspace"] == expected_cwd


# ---------------------------------------------------------------------------
# Negative twins: cwd falls back to workspace root
# ---------------------------------------------------------------------------


def test_zero_github_mounts_falls_back_to_workspace_root(client: TestClient, fake_launcher) -> None:
    """File-only session → cwd is workspace root (no repo to cd into)."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "file",
                "content": "x\n",
                "mount_path": "/workspace/data/in.txt",
            }
        ],
    }
    created = _create_session(client, payload)
    workspace_root = Path(created["workspace"])
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["workspace"] == workspace_root, (
        f"file-only session must launch at workspace root; "
        f"got {fake_launcher.calls[0]['workspace']!r}"
    )


def test_multiple_github_mounts_falls_back_to_workspace_root(
    client: TestClient, fake_launcher, tmp_path: Path
) -> None:
    """Two github mounts and no explicit working_directory → workspace root, no heuristic guess."""
    repo_a = _bare_repo(tmp_path, "a.git")
    repo_b = _bare_repo(tmp_path, "b.git")
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{repo_a}",
                "mount_path": "/workspace/a",
            },
            {
                "type": "github_repository",
                "url": f"file://{repo_b}",
                "mount_path": "/workspace/b",
            },
        ],
    }
    created = _create_session(client, payload)
    workspace_root = Path(created["workspace"])
    _send_and_wait(client, fake_launcher, created["session_id"])

    assert fake_launcher.calls[0]["workspace"] == workspace_root, (
        f"multi-mount session must default to workspace root; "
        f"got {fake_launcher.calls[0]['workspace']!r}"
    )


def test_explicit_working_directory_escaping_workspace_returns_400(
    client: TestClient, bare_repo: Path
) -> None:
    """working_directory='/etc/passwd' (or other escape) → 400, no session created."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "working_directory": "/etc/passwd",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400, r.text
    assert "escapes workspace" in r.json()["detail"]


def test_explicit_working_directory_with_dot_dot_segments_returns_400(
    client: TestClient, bare_repo: Path
) -> None:
    """Negative twin: ``..`` segments that escape the workspace are also rejected."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "working_directory": "/workspace/../../etc",
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    r = client.post("/v1/sessions", json=payload)
    assert r.status_code == 400
    assert "escapes workspace" in r.json()["detail"]


# ---------------------------------------------------------------------------
# session.created event payload + persisted log
# ---------------------------------------------------------------------------


def test_session_created_event_records_working_directory(
    client: TestClient, fake_launcher, bare_repo: Path, tmp_sessions_dir: Path
) -> None:
    """The session.created JSONL event must carry the resolved working_directory
    so a rehydrated session can recover it (preserves correctness across
    process restarts — hard rule 6)."""
    payload = {
        "agent": {"name": "t", "system": "s", "provider": "fake_scripted"},
        "resources": [
            {
                "type": "github_repository",
                "url": f"file://{bare_repo}",
                "mount_path": "/workspace/repo",
            }
        ],
    }
    created = _create_session(client, payload)
    session_id = created["session_id"]
    expected_cwd = created["resources_mounted"][0]["local_path"]

    log_path = tmp_sessions_dir / f"{session_id}.jsonl"
    assert log_path.exists()
    created_events = [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if json.loads(line).get("type") == "session.created"
    ]
    assert len(created_events) == 1
    assert created_events[0].get("working_directory") == expected_cwd


# ---------------------------------------------------------------------------
# Rehydration: legacy logs (no working_directory field on session.created)
# ---------------------------------------------------------------------------


def test_rehydration_legacy_session_created_event_has_empty_working_directory() -> None:
    """A legacy session.created event with no working_directory key must rehydrate
    cleanly — Session.working_directory falls back to workspace via __post_init__
    so existing on-disk sessions keep working after this change."""
    legacy_events = [
        {
            "event_id": "019e1bad-e4bf-7232-a49e-37840590637e",
            "type": "session.created",
            "timestamp": "2026-05-12T10:14:01.663304+00:00",
            "agent": "legacy-agent",
        }
    ]
    session = rehydrate_from_events("sesn_legacy", legacy_events)
    # workspace is unrecoverable from event log (was never persisted) — empty string.
    # working_directory falls back to that same empty string; the entity does not
    # invent a path it never knew.
    assert session.working_directory == session.workspace == ""


def test_rehydration_modern_session_created_event_carries_working_directory() -> None:
    """A new session.created event with working_directory must surface on the
    rehydrated Session."""
    events = [
        {
            "event_id": "019e2b3f-7626-741c-a6e1-639992219aee",
            "type": "session.created",
            "timestamp": "2026-05-15T10:47:19.846510+00:00",
            "agent": "modern-agent",
            "working_directory": "/tmp/mad_sesn_modern/repo",
        }
    ]
    session = rehydrate_from_events("sesn_modern", events)
    assert session.working_directory == "/tmp/mad_sesn_modern/repo"


# ---------------------------------------------------------------------------
# OpenAPI contract — heuristic 5
# ---------------------------------------------------------------------------


def _resolve_ref(spec: dict, ref: str) -> dict:
    name = ref.rsplit("/", 1)[-1]
    return spec["components"]["schemas"][name]


def test_openapi_create_session_declares_working_directory_field(
    client: TestClient,
) -> None:
    """The new optional field must appear in the OpenAPI schema for
    CreateSessionRequest so clients see it in /docs and Postman."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions"]["post"]
    schema_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    props = component["properties"]
    assert "working_directory" in props, (
        f"CreateSessionRequest is missing working_directory; declared: {sorted(props)}"
    )
    # The field is optional — must NOT appear in required.
    required = set(component.get("required", []))
    assert "working_directory" not in required


def test_openapi_create_session_working_directory_is_optional_string(
    client: TestClient,
) -> None:
    """Type contract: working_directory is a nullable string (str | None = None)."""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/sessions"]["post"]
    schema_ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    component = _resolve_ref(spec, schema_ref)
    wd = component["properties"]["working_directory"]
    # Pydantic v2 emits anyOf for str | None
    any_of = wd.get("anyOf")
    assert any_of is not None, f"expected anyOf for nullable string, got {wd!r}"
    types = {entry.get("type") for entry in any_of}
    assert types == {"string", "null"}, f"unexpected types: {types}"
