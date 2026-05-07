"""Unit tests for ``mad.core.events.domain.event.Event``.

Verifies the entity is a frozen, equality-comparable record that
tolerates the legacy ``event_id is None`` case from ADR-0005.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from mad.core.events.domain.event import Event
from mad.core.events.domain.event_id import new_event_id


def _sample(**overrides: object) -> Event:
    base: dict[str, object] = {
        "event_id": new_event_id(),
        "session_id": "sesn_abc",
        "type": "agent.output",
        "data": {"line": "hello"},
        "timestamp": datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
    }
    base.update(overrides)
    return Event(**base)  # type: ignore[arg-type]


def test_event_is_frozen() -> None:
    event = _sample()
    with pytest.raises(FrozenInstanceError):
        event.session_id = "other"  # type: ignore[misc]


def test_event_id_may_be_none_for_legacy_records() -> None:
    legacy = _sample(event_id=None)
    assert legacy.event_id is None


def test_equality_is_value_based() -> None:
    eid = new_event_id()
    a = _sample(event_id=eid)
    b = _sample(event_id=eid)
    assert a == b
