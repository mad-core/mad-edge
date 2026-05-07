"""JSONL-backed ``EventLogQuery`` implementation.

Reads from the same ``sessions/*.jsonl`` files that
``JsonlSessionRepository`` writes (CLAUDE.md hard rule 6 — single source
of truth). For v1 Mad volume the implementation loads + sorts in
memory; ADR-0004 records the migration path to a streaming/indexed
store when this becomes a hotspot.

Sort key is the textual ``event_id`` (UUIDv7 → lex order = mint order
across milliseconds). Pre-existing events without an ``event_id`` sort
first (treated as "older than any known id"), per ADR-0005.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from mad.adapters.outbound.persistence import jsonl_session_repository as _persistence
from mad.core.events.domain.event import Event, event_from_persisted
from mad.core.events.ports.event_log_query import EventQuery


class JsonlEventLogQuery:
    """Read-side query over ``sessions/*.jsonl``."""

    def __init__(self, sessions_dir: Path | None = None) -> None:
        self._explicit_sessions_dir = sessions_dir

    @property
    def _sessions_dir(self) -> Path:
        # Read SESSIONS_DIR lazily so tests that monkeypatch it after
        # construction (via tmp_sessions_dir) still take effect.
        if self._explicit_sessions_dir is not None:
            return self._explicit_sessions_dir
        return _persistence.SESSIONS_DIR

    def query(self, q: EventQuery) -> list[Event]:
        events = [e for e in self._all_events() if _matches(e, q)]
        events.sort(key=_sort_key)
        return events[: q.limit]

    def session_ids_for_agent(self, agent_name: str) -> frozenset[str]:
        return frozenset(
            event.session_id
            for event in self._all_events()
            if event.type == "session.created" and event.data.get("agent") == agent_name
        )

    def _all_events(self) -> Iterator[Event]:
        if not self._sessions_dir.exists():
            return
        for path in sorted(self._sessions_dir.glob("*.jsonl")):
            session_id = path.stem
            for raw_line in path.read_text().splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                yield event_from_persisted(json.loads(line), session_id)


def _matches(event: Event, q: EventQuery) -> bool:
    if q.session_id is not None and event.session_id != q.session_id:
        return False
    if q.kind is not None and event.type != q.kind:
        return False
    if q.session_ids_for_agent is not None and event.session_id not in q.session_ids_for_agent:
        return False
    if q.since is not None and event.timestamp < q.since:
        return False
    return not (
        q.after_event_id is not None
        and (event.event_id is None or event.event_id <= q.after_event_id)
    )


def _sort_key(event: Event) -> tuple[str, datetime]:
    """Lex-sortable key. Legacy events without an id sort first."""
    eid_str = str(event.event_id) if event.event_id is not None else ""
    return (eid_str, event.timestamp)
