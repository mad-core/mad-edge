"""Startup wiring for JSONL log retention (issue #14).

The app lifespan enforces the optional TTL once at startup: when
``MAD_SESSIONS_RETENTION_DAYS`` is set, expired per-session logs are purged;
when it is unset (the safe default), nothing is purged. These tests drive the
real ``create_app`` lifespan via the ``TestClient`` context manager and assert
on the filesystem — a stale log present before startup, gone (or kept) after.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http import create_app
from mad.adapters.outbound.persistence.jsonl_session_repository import RETENTION_DAYS_ENV
from mad.core.events.domain.event_id import new_event_id


def _write_stale_log(sessions_dir: Path, session_id: str, days_old: int) -> Path:
    path = sessions_dir / f"{session_id}.jsonl"
    ts = (datetime.now(UTC) - timedelta(days=days_old)).isoformat()
    path.write_text(
        json.dumps({"event_id": str(new_event_id()), "type": "session.deleted", "timestamp": ts})
        + "\n"
    )
    return path


def test_startup_purges_expired_log_when_retention_set(
    tmp_sessions_dir: Path,
    tmp_workspaces_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "30")
    stale = _write_stale_log(tmp_sessions_dir, "expired-session", days_old=31)

    app = create_app()
    with TestClient(app):
        pass  # entering/exiting the context runs the lifespan

    assert not stale.exists()


def test_startup_keeps_expired_log_when_retention_unset(
    tmp_sessions_dir: Path,
    tmp_workspaces_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative twin: no env var -> retention disabled -> stale log survives startup."""
    monkeypatch.delenv(RETENTION_DAYS_ENV, raising=False)
    stale = _write_stale_log(tmp_sessions_dir, "expired-session", days_old=31)

    app = create_app()
    with TestClient(app):
        pass

    assert stale.exists()


def test_startup_keeps_recent_log_when_retention_set(
    tmp_sessions_dir: Path,
    tmp_workspaces_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative twin: a log within the window survives even with retention on."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "30")
    recent = _write_stale_log(tmp_sessions_dir, "recent-session", days_old=2)

    app = create_app()
    with TestClient(app):
        pass

    assert recent.exists()
