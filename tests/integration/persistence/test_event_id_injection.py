"""Integration tests for ADR-0005 — UUIDv7 ``event_id`` injection.

Every event written through ``JsonlSessionRepository.append_event``
must carry a valid UUIDv7 ``event_id``, and ids minted across multiple
events must be lex-sortable in mint order.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import UUID

import pytest

from mad.adapters.outbound.persistence.jsonl_session_repository import (
    JsonlSessionRepository,
    log_path,
)


@pytest.fixture
def repo(tmp_sessions_dir: Path) -> JsonlSessionRepository:
    return JsonlSessionRepository()


def _read_lines(session_id: str) -> list[dict]:
    return [json.loads(ln) for ln in log_path(session_id).read_text().splitlines() if ln]


def test_append_event_writes_event_id_field(repo: JsonlSessionRepository) -> None:
    returned = repo.append_event("sesn_a", "session.created", {"agent": "claude_cli"})
    persisted = _read_lines("sesn_a")

    assert "event_id" in returned
    assert len(persisted) == 1
    assert persisted[0]["event_id"] == returned["event_id"]


def test_persisted_event_id_is_valid_uuid_v7(repo: JsonlSessionRepository) -> None:
    repo.append_event("sesn_a", "agent.output", {"line": "hello"})

    persisted = _read_lines("sesn_a")[0]
    parsed = UUID(persisted["event_id"])

    assert parsed.version == 7
    assert parsed.variant == "specified in RFC 4122"


def test_event_ids_are_distinct_across_rapid_calls(
    repo: JsonlSessionRepository,
) -> None:
    """Within a single millisecond the 74 random bits make collisions
    astronomically unlikely (cross-ms ordering is covered by the next test
    and by ADR-0005)."""
    for i in range(50):
        repo.append_event("sesn_a", "agent.output", {"line": f"line {i}"})

    persisted = _read_lines("sesn_a")
    ids = [e["event_id"] for e in persisted]

    assert len(set(ids)) == 50


def test_event_ids_lex_sort_matches_mint_order_across_milliseconds(
    repo: JsonlSessionRepository,
) -> None:
    """UUIDv7 is monotonic at millisecond granularity (ADR-0005). Two
    events minted in the same ms are not guaranteed to lex-sort in
    mint order; events more than 1 ms apart are."""
    for i in range(5):
        repo.append_event("sesn_a", "agent.output", {"line": f"line {i}"})
        time.sleep(0.002)

    persisted = _read_lines("sesn_a")
    ids = [e["event_id"] for e in persisted]

    assert ids == sorted(ids)


def test_event_id_does_not_clobber_caller_supplied_keys(
    repo: JsonlSessionRepository,
) -> None:
    """Event payload data keeps its own keys; only ``event_id`` is added."""
    repo.append_event("sesn_a", "agent.output", {"line": "x", "custom": 1})

    persisted = _read_lines("sesn_a")[0]
    assert persisted["line"] == "x"
    assert persisted["custom"] == 1
    assert persisted["type"] == "agent.output"
    assert "event_id" in persisted
