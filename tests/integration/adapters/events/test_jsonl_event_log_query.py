"""Integration tests for ``JsonlEventLogQuery``.

Writes real JSONL session files into a temp directory via
``JsonlSessionRepository`` (the canonical writer) so the format under
test is exactly what production produces. Verifies cross-session sort,
each filter dimension, ``Last-Event-ID`` catch-up, pagination, and
the legacy-event-without-id surface.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from mad.adapters.outbound.events.jsonl_event_log_query import JsonlEventLogQuery
from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
    log_path,
)
from mad.core.events.ports.event_log_query import EventQuery


@pytest.fixture
def repo(tmp_sessions_dir: Path) -> JsonlSessionRepository:
    return JsonlSessionRepository()


@pytest.fixture
def query(tmp_sessions_dir: Path) -> JsonlEventLogQuery:
    # No explicit dir — exercise the SESSIONS_DIR lookup path that
    # production uses, with the conftest monkeypatch in effect.
    return JsonlEventLogQuery()


def test_query_returns_events_across_sessions_in_event_id_order(
    repo: JsonlSessionRepository, query: JsonlEventLogQuery
) -> None:
    repo.append_event("sesn_a", "session.created", {"agent": "claude_cli"})
    time.sleep(0.002)
    repo.append_event("sesn_b", "session.created", {"agent": "claude_cli"})
    time.sleep(0.002)
    repo.append_event("sesn_a", "agent.output", {"line": "hi"})

    events = query.query(EventQuery(limit=100))
    ids = [str(e.event_id) for e in events]

    assert ids == sorted(ids)
    assert {e.session_id for e in events} == {"sesn_a", "sesn_b"}
    assert len(events) == 3


def test_filter_by_session_id(repo: JsonlSessionRepository, query: JsonlEventLogQuery) -> None:
    repo.append_event("sesn_a", "agent.output", {"line": "a"})
    repo.append_event("sesn_b", "agent.output", {"line": "b"})

    events = query.query(EventQuery(session_id="sesn_a"))

    assert {e.session_id for e in events} == {"sesn_a"}
    assert events[0].data["line"] == "a"


def test_filter_by_kind(repo: JsonlSessionRepository, query: JsonlEventLogQuery) -> None:
    repo.append_event("sesn_a", "agent.output", {"line": "x"})
    repo.append_event("sesn_a", "session.status_idle", None)

    events = query.query(EventQuery(kind="session.status_idle"))

    assert [e.type for e in events] == ["session.status_idle"]


def test_filter_after_event_id_for_last_event_id_catchup(
    repo: JsonlSessionRepository, query: JsonlEventLogQuery
) -> None:
    e1 = repo.append_event("sesn_a", "agent.output", {"line": "1"})
    time.sleep(0.002)
    e2 = repo.append_event("sesn_a", "agent.output", {"line": "2"})
    time.sleep(0.002)
    e3 = repo.append_event("sesn_a", "agent.output", {"line": "3"})

    events = query.query(EventQuery(after_event_id=UUID(e1["event_id"])))

    assert [str(e.event_id) for e in events] == [e2["event_id"], e3["event_id"]]


def test_filter_since_timestamp(repo: JsonlSessionRepository, query: JsonlEventLogQuery) -> None:
    repo.append_event("sesn_a", "agent.output", {"line": "old"})
    cutoff = datetime.now(UTC) + timedelta(seconds=1)
    repo.append_event("sesn_a", "agent.output", {"line": "still-old"})

    events = query.query(EventQuery(since=cutoff))

    assert events == []


def test_limit_caps_results(repo: JsonlSessionRepository, query: JsonlEventLogQuery) -> None:
    for i in range(5):
        repo.append_event("sesn_a", "agent.output", {"line": f"line {i}"})
        time.sleep(0.002)

    events = query.query(EventQuery(limit=2))

    assert len(events) == 2


def test_session_ids_for_agent_resolves_from_session_created_events(
    repo: JsonlSessionRepository, query: JsonlEventLogQuery
) -> None:
    repo.append_event("sesn_a", "session.created", {"agent": "claude_cli"})
    repo.append_event("sesn_b", "session.created", {"agent": "claude_cli"})
    repo.append_event("sesn_c", "session.created", {"agent": "fake"})
    repo.append_event("sesn_a", "agent.output", {"line": "noise"})

    ids = query.session_ids_for_agent("claude_cli")

    assert ids == frozenset({"sesn_a", "sesn_b"})


def test_legacy_events_without_event_id_surface_as_none(
    tmp_sessions_dir: Path, query: JsonlEventLogQuery
) -> None:
    """Per ADR-0005 — JSONL lines written before this PR have no
    event_id; the query layer surfaces them with event_id=None and
    sorts them before any UUIDv7."""
    legacy = log_path("sesn_legacy")
    legacy.write_text(
        json.dumps(
            {
                "type": "session.status_idle",
                "timestamp": "2026-04-01T12:00:00+00:00",
            }
        )
        + "\n"
    )

    events = query.query(EventQuery())

    assert len(events) == 1
    assert events[0].event_id is None
    assert events[0].session_id == "sesn_legacy"
    assert events[0].type == "session.status_idle"


def test_filter_by_session_ids_for_agent(
    repo: JsonlSessionRepository, query: JsonlEventLogQuery
) -> None:
    repo.append_event("sesn_a", "session.created", {"agent": "claude_cli"})
    repo.append_event("sesn_b", "session.created", {"agent": "claude_cli"})
    repo.append_event("sesn_c", "session.created", {"agent": "other"})
    repo.append_event("sesn_a", "agent.output", {"line": "from a"})
    repo.append_event("sesn_c", "agent.output", {"line": "from c"})

    events = query.query(
        EventQuery(
            kind="agent.output",
            session_ids_for_agent=frozenset({"sesn_a", "sesn_b"}),
        )
    )

    assert [e.session_id for e in events] == ["sesn_a"]
