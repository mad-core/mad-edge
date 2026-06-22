from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mad.core.events.domain.event import Event, event_from_persisted
from mad.core.events.domain.event_id import new_event_id

#: Environment variable an operator sets to relocate the session log directory.
SESSIONS_DIR_ENV = "MAD_SESSIONS_DIR"

#: Fallback used when ``MAD_SESSIONS_DIR`` is unset (local dev unchanged).
DEFAULT_SESSIONS_DIR = Path("sessions")

RETENTION_DAYS_ENV = "MAD_SESSIONS_RETENTION_DAYS"

# ---------------------------------------------------------------------------
# Free functions (module-level API)
# ---------------------------------------------------------------------------


def sessions_dir() -> Path:
    """Resolve the session log directory dynamically.

    Reads ``MAD_SESSIONS_DIR`` from the environment on every call so an
    operator override is honored at runtime (and tests can override it),
    falling back to ``Path("sessions")`` when the variable is unset or
    blank. This is intentionally a function, not a module-import-time
    constant: importing the module must not freeze the resolution.
    """
    override = os.environ.get(SESSIONS_DIR_ENV, "").strip()
    if override:
        return Path(override)
    return DEFAULT_SESSIONS_DIR


def ensure_sessions_dir() -> None:
    sessions_dir().mkdir(parents=True, exist_ok=True)


def log_path(session_id: str) -> Path:
    return sessions_dir() / f"{session_id}.jsonl"


def emit(session_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict:
    """Print an event to stdout AND append it to the session JSONL log.

    The log is the source of truth (CLAUDE.md hard rule 6). Each event
    carries a UUIDv7 ``event_id`` so cross-session ordering and SSE
    ``Last-Event-ID`` catch-up work without a parallel store (ADR-0005).
    """
    event = {
        "event_id": str(new_event_id()),
        "type": event_type,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    if data:
        event.update(data)
    line = json.dumps(event)
    print(line)
    ensure_sessions_dir()
    with log_path(session_id).open("a") as f:
        f.write(line + "\n")
    return event


def get_events(session_id: str) -> list[dict]:
    p = log_path(session_id)
    if not p.exists():
        return []
    events: list[dict] = []
    for ln in p.read_text().splitlines():
        ln = ln.strip()
        if ln:
            events.append(json.loads(ln))
    return events


def resolve_retention_days() -> int | None:
    """Read the configured JSONL log retention window from the environment.

    Returns the positive number of days an operator set via
    ``MAD_SESSIONS_RETENTION_DAYS``. Unset, non-integer, zero, or negative
    values all resolve to ``None``, which the caller treats as "retention
    disabled — keep every log forever" (the safe default; no behavior change
    over the historical never-purge contract, issue #14).
    """
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw is None:
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    return days if days > 0 else None


def _last_event_timestamp(path: Path) -> datetime | None:
    """Return the parsed ``timestamp`` of the LAST event in a JSONL log.

    A log with no parseable timestamped event returns ``None`` so the caller
    keeps it (a half-written or empty file is never old enough to purge).
    """
    last: datetime | None = None
    for ln in path.read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            raw = json.loads(ln).get("timestamp")
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        last = parsed
    return last


def purge_expired_logs(now: datetime, retention_days: int) -> list[str]:
    """Delete per-session JSONL logs whose LAST event predates the cutoff.

    A log is purged when its most-recent event ``timestamp`` is strictly
    older than ``now - retention_days``. The last event is used (not the
    first) so an actively-appended log is never deleted out from under a
    live session — the cutoff tracks the most recent activity (issue #14).

    ``retention_days <= 0`` is a no-op (returns ``[]``): callers gate on
    :func:`resolve_retention_days` returning ``None`` to disable purging,
    but this guard keeps the primitive safe if called directly.

    Reserved internal streams (ids starting with ``__``, e.g. the
    deployment-wide policy log) are never purged — they mirror the exclusion
    in :meth:`JsonlSessionRepository.list_session_ids`.

    Returns the session ids whose logs were deleted.
    """
    if retention_days <= 0:
        return []
    root = sessions_dir()
    if not root.exists():
        return []

    cutoff = now - timedelta(days=retention_days)
    purged: list[str] = []
    for path in sorted(root.glob("*.jsonl")):
        if path.stem.startswith("__"):
            continue
        last = _last_event_timestamp(path)
        if last is None:
            continue
        if last < cutoff:
            path.unlink()
            purged.append(path.stem)
    return purged


class JsonlSessionRepository:
    """Concrete implementation of ``SessionRepository`` backed by JSONL files.

    Delegates to the free functions above so callers that still use the
    module-level API continue to work unchanged.
    """

    def append_event(
        self,
        session_id: str,
        event_type: str,
        data: dict[str, Any] | None = None,
    ) -> dict:
        """Append an event and return the serialised event dict."""
        return emit(session_id, event_type, data)

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """Satisfy ``EventStore`` — persist and return a typed ``Event``."""
        raw = self.append_event(session_id, type, data)
        return event_from_persisted(raw, session_id)

    def read_events(self, session_id: str) -> list[dict]:
        """Return all events recorded for the session."""
        return get_events(session_id)

    def exists(self, session_id: str) -> bool:
        """Return True if any events have been persisted for the session."""
        return log_path(session_id).exists()

    def list_session_ids(self) -> list[str]:
        """Return every session ID with a persisted JSONL log on disk.

        Reserved internal streams (ids starting with ``__``, e.g. the
        deployment-wide dispatch-policy log from issue #45) are NOT real
        sessions and are excluded so they never get rehydrated or listed.
        """
        root = sessions_dir()
        if not root.exists():
            return []
        return sorted(p.stem for p in root.glob("*.jsonl") if not p.stem.startswith("__"))
