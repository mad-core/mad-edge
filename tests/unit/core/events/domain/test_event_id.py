"""Unit tests for ``mad.core.events.domain.event_id.new_event_id``.

Verifies the load-bearing properties from ADR-0005: the minted UUIDs
are version 7, variant RFC 4122, and lexicographically sortable across
distinct millisecond timestamps.
"""

from __future__ import annotations

import time

from mad.core.events.domain.event_id import new_event_id


def test_returns_uuid_version_7_variant_rfc4122() -> None:
    eid = new_event_id()

    assert eid.version == 7
    # RFC 4122 variant is the high two bits = 0b10. The Python
    # ``UUID.variant`` property reports it as the string ``"specified in
    # RFC 4122"``.
    assert eid.variant == "specified in RFC 4122"


def test_timestamp_prefix_matches_current_unix_ms() -> None:
    before_ms = int(time.time() * 1000)
    eid = new_event_id()
    after_ms = int(time.time() * 1000)

    # First 48 bits = unix-ms timestamp.
    embedded_ms = eid.int >> 80

    assert before_ms <= embedded_ms <= after_ms


def test_lex_sort_matches_mint_order_across_milliseconds() -> None:
    ids: list[str] = []
    for _ in range(5):
        ids.append(str(new_event_id()))
        # Force at least one ms gap so the timestamp prefix advances.
        time.sleep(0.002)

    assert ids == sorted(ids)


def test_distinct_calls_yield_distinct_ids() -> None:
    # Even within the same millisecond, the 74 random bits make
    # collisions astronomically unlikely.
    ids = {new_event_id() for _ in range(1000)}
    assert len(ids) == 1000
