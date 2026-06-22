"""JSONL log retention/rotation tests (issue #14).

Covers the two adapter primitives:

- ``purge_expired_logs(now, retention_days)`` — deletes per-session logs whose
  LAST event timestamp predates ``now - retention_days``.
- ``resolve_retention_days()`` — reads ``MAD_SESSIONS_RETENTION_DAYS``; unset /
  non-positive / non-integer all mean "disabled".

Each happy-path case has a negative twin (testing heuristic rule 1): a purge
that deletes is paired with a recent log that is kept and a disabled-retention
run that purges nothing; an env value that enables is paired with the unset /
zero / garbage values that disable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from mad.adapters.outbound.persistence.jsonl_session_repository import (
    RETENTION_DAYS_ENV,
    purge_expired_logs,
    resolve_retention_days,
)

NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)


def _write_log(sessions_dir: Path, session_id: str, last_event_at: datetime) -> Path:
    """Materialize a JSONL log whose final event carries ``last_event_at``."""
    path = sessions_dir / f"{session_id}.jsonl"
    lines = [
        json.dumps(
            {"event_id": "e1", "type": "session.created", "timestamp": "2020-01-01T00:00:00+00:00"}
        ),
        json.dumps(
            {"event_id": "e2", "type": "session.deleted", "timestamp": last_event_at.isoformat()}
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# purge_expired_logs — positive then negative twins
# ---------------------------------------------------------------------------


def test_purge_deletes_log_whose_last_event_is_older_than_cutoff(
    tmp_sessions_dir: Path,
) -> None:
    expired = _write_log(tmp_sessions_dir, "old-session", NOW - timedelta(days=31))

    purged = purge_expired_logs(NOW, retention_days=30)

    assert purged == ["old-session"]
    assert not expired.exists()


def test_purge_keeps_log_whose_last_event_is_within_retention(
    tmp_sessions_dir: Path,
) -> None:
    """Negative twin: a recent log is retained and not reported as purged."""
    recent = _write_log(tmp_sessions_dir, "fresh-session", NOW - timedelta(days=2))

    purged = purge_expired_logs(NOW, retention_days=30)

    assert purged == []
    assert recent.exists()


def test_purge_uses_last_event_not_first(tmp_sessions_dir: Path) -> None:
    """A log whose FIRST event is ancient but whose LAST event is recent stays.

    This is the load-bearing distinction: retention tracks most-recent activity
    so an actively-appended log is never deleted out from under a live session.
    """
    recent = _write_log(tmp_sessions_dir, "long-lived", NOW - timedelta(days=1))

    purged = purge_expired_logs(NOW, retention_days=30)

    assert purged == []
    assert recent.exists()


def test_purge_is_noop_for_non_positive_retention(tmp_sessions_dir: Path) -> None:
    """Negative twin: retention_days <= 0 deletes nothing even for ancient logs."""
    ancient = _write_log(tmp_sessions_dir, "ancient", NOW - timedelta(days=999))

    purged = purge_expired_logs(NOW, retention_days=0)

    assert purged == []
    assert ancient.exists()


def test_purge_skips_reserved_internal_streams(tmp_sessions_dir: Path) -> None:
    """Reserved ``__`` streams are never purged even when expired."""
    reserved = _write_log(tmp_sessions_dir, "__dispatch_policy", NOW - timedelta(days=999))

    purged = purge_expired_logs(NOW, retention_days=30)

    assert purged == []
    assert reserved.exists()


def test_purge_keeps_log_with_no_parseable_timestamp(tmp_sessions_dir: Path) -> None:
    """A half-written log with no parseable timestamped event is never old enough."""
    path = tmp_sessions_dir / "partial.jsonl"
    path.write_text("{ this is not valid json\n")

    purged = purge_expired_logs(NOW, retention_days=1)

    assert purged == []
    assert path.exists()


# ---------------------------------------------------------------------------
# resolve_retention_days — positive then negative twins
# ---------------------------------------------------------------------------


def test_resolve_returns_positive_int_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RETENTION_DAYS_ENV, "45")

    assert resolve_retention_days() == 45


def test_resolve_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin: unset env var disables retention (None)."""
    monkeypatch.delenv(RETENTION_DAYS_ENV, raising=False)

    assert resolve_retention_days() is None


def test_resolve_returns_none_for_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin: an explicit 0 disables retention (None), never purges."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "0")

    assert resolve_retention_days() is None


def test_resolve_returns_none_for_negative(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin: a negative window disables retention (None)."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "-7")

    assert resolve_retention_days() is None


def test_resolve_returns_none_for_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin: a non-integer value disables retention rather than crashing."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "thirty")

    assert resolve_retention_days() is None
