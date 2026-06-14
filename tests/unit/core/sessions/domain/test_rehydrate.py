"""Unit tests for ``rehydrate_from_events``.

The rehydration helper drives both ``GetSessionUseCase`` (when a session
is missing from memory) and ``ListSessionsUseCase`` (when listings span
restarts). Issue #17 added timestamps — this file pins the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

from mad.core.orchestration.domain.dispatch_policy import ManualPolicy
from mad.core.sessions.domain.rehydrate import rehydrate_from_events


def test_rehydrate_uses_session_created_event_for_created_at() -> None:
    """``created_at`` is taken from the ``session.created`` event, not the
    earliest event in the log — out-of-order writes of ancestor events
    must not corrupt the timestamp.
    """
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-05-06T09:00:00+00:00",
            "agent": "t",
        },
        {
            "type": "session.status_running",
            "timestamp": "2026-05-06T09:05:00+00:00",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.created_at == datetime(2026, 5, 6, 9, 0, tzinfo=UTC)


def test_rehydrate_updated_at_is_latest_event_timestamp() -> None:
    """``updated_at`` is the maximum event timestamp — that's how filters
    on ``updated_after`` find sessions whose last activity is recent.
    """
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-05-06T09:00:00+00:00",
            "agent": "t",
        },
        {
            "type": "agent.output",
            "timestamp": "2026-05-06T09:30:00+00:00",
        },
        {
            "type": "session.status_idle",
            "timestamp": "2026-05-06T10:00:00+00:00",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.updated_at == datetime(2026, 5, 6, 10, 0, tzinfo=UTC)
    assert s.status == "idle"


def test_rehydrate_skips_unparseable_timestamps_but_preserves_status() -> None:
    """A bad timestamp on one event must not crash rehydration nor pull
    ``updated_at`` to the wrong value; the well-formed events still drive
    the timestamp and status.
    """
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-05-06T09:00:00+00:00",
            "agent": "t",
        },
        {"type": "agent.output", "timestamp": "not-a-real-timestamp"},
        {
            "type": "session.status_idle",
            "timestamp": "2026-05-06T09:30:00+00:00",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.status == "idle"
    assert s.created_at == datetime(2026, 5, 6, 9, 0, tzinfo=UTC)
    assert s.updated_at == datetime(2026, 5, 6, 9, 30, tzinfo=UTC)


def test_rehydrate_dispatch_policy_cleared_resets_override_to_none() -> None:
    """Issue #45: replaying ``dispatch_policy.cleared`` after a prior
    ``dispatch_policy.updated`` must drop the per-session override back to
    ``None`` so the session inherits the deployment default again."""
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-05-06T09:00:00+00:00",
            "agent": "t",
        },
        {
            "type": "dispatch_policy.updated",
            "timestamp": "2026-05-06T09:05:00+00:00",
            "kind": "manual",
        },
        {
            "type": "dispatch_policy.cleared",
            "timestamp": "2026-05-06T09:10:00+00:00",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.dispatch_policy is None


def test_rehydrate_dispatch_policy_without_clear_keeps_override() -> None:
    """Negative twin: with no ``dispatch_policy.cleared`` in the log the
    override survives — clearing only resets when the event is present."""
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-05-06T09:00:00+00:00",
            "agent": "t",
        },
        {
            "type": "dispatch_policy.updated",
            "timestamp": "2026-05-06T09:05:00+00:00",
            "kind": "manual",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert isinstance(s.dispatch_policy, ManualPolicy)


def test_rehydrate_empty_events_yields_epoch_timestamps() -> None:
    """An empty event log should not crash; both timestamps fall back to
    the Unix epoch in UTC so they sort first in any listing.
    """
    s = rehydrate_from_events("sesn_x", [])

    assert s.created_at == datetime.fromtimestamp(0, tz=UTC)
    assert s.updated_at == s.created_at
    assert s.status == "created"


# -- dispatch_priority.updated replay (issue #46) -------------------------------


def test_rehydrate_replays_latest_dispatch_priority() -> None:
    """``Session.priority`` is durable ONLY as the replayed event — the
    last ``dispatch_priority.updated`` in the log wins."""
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-06-01T09:00:00+00:00",
            "agent": "t",
        },
        {
            "type": "dispatch_priority.updated",
            "timestamp": "2026-06-01T09:01:00+00:00",
            "priority": 3,
        },
        {
            "type": "dispatch_priority.updated",
            "timestamp": "2026-06-01T09:02:00+00:00",
            "priority": 9,
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.priority == 9


def test_rehydrate_without_priority_event_defaults_to_lowest() -> None:
    """Negative twin: a session never prioritized replays to priority 1
    (the lowest) — an explicitly prioritized session always outranks it."""
    events = [
        {
            "type": "session.created",
            "timestamp": "2026-06-01T09:00:00+00:00",
            "agent": "t",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.priority == 1


def test_rehydrate_skips_malformed_priority_payloads() -> None:
    """Out-of-range or non-int payloads (hand-edited logs) must not poison
    the replay: the previous valid value is kept, mirroring how malformed
    dispatch_policy payloads are skipped."""
    events = [
        {
            "type": "dispatch_priority.updated",
            "timestamp": "2026-06-01T09:01:00+00:00",
            "priority": 4,
        },
        {
            "type": "dispatch_priority.updated",
            "timestamp": "2026-06-01T09:02:00+00:00",
            "priority": 42,
        },
        {
            "type": "dispatch_priority.updated",
            "timestamp": "2026-06-01T09:03:00+00:00",
            "priority": "high",
        },
    ]
    s = rehydrate_from_events("sesn_x", events)

    assert s.priority == 4
