from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mad.core.events.domain.event import Event, event_from_persisted
from mad.core.events.domain.event_id import new_event_id

#: Environment variable an operator sets to relocate the session log directory.
SESSIONS_DIR_ENV = "MAD_SESSIONS_DIR"

#: Fallback used when ``MAD_SESSIONS_DIR`` is unset (local dev unchanged).
DEFAULT_SESSIONS_DIR = Path("sessions")

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
