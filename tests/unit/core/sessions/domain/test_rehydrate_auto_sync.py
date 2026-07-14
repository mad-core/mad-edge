"""Unit tests for ``auto_sync`` rehydration from the session log (issue #109).

The session event log is the source of truth (hard rule 6): after a crash the
harness rebuilds ``Session`` from its JSONL. If ``auto_sync`` did not survive the
round-trip, a session that opted out would come back with the gate ON and the
post-run publish would re-open the duplicate PR on the next idle — so the
``session.created`` payload has to carry it and ``rehydrate_from_events`` has to
read it back.

``False`` and "absent" are distinct outcomes: absent means "no per-session
override, inherit ``MAD_AUTO_SYNC`` > ``True``", which is what keeps pre-#109
logs replaying with their original behaviour.
"""

from __future__ import annotations

from typing import Any

from mad.core.sessions.domain.rehydrate import rehydrate_from_events


def _created_event(**extra: Any) -> dict[str, Any]:
    return {
        "type": "session.created",
        "timestamp": "2026-06-01T12:00:00+00:00",
        "agent": "t",
        "working_directory": "/workspace",
        **extra,
    }


def test_rehydrate_reads_auto_sync_false_from_session_created() -> None:
    """An opted-out session survives a replay as opted out."""
    session = rehydrate_from_events("sesn_a", [_created_event(auto_sync=False)])
    assert session.auto_sync is False


def test_rehydrate_reads_auto_sync_true_from_session_created() -> None:
    """Negative twin: an explicit opt-IN is also preserved verbatim, so the False
    above is a read value and not a blanket falsey default."""
    session = rehydrate_from_events("sesn_b", [_created_event(auto_sync=True)])
    assert session.auto_sync is True


def test_rehydrate_auto_sync_is_none_when_key_absent() -> None:
    """Negative twin: a pre-#109 ``session.created`` (no ``auto_sync`` key) yields
    ``None`` — "inherit the operator default", NOT ``False``. Replaying an old log
    must not silently disable the safety net."""
    session = rehydrate_from_events("sesn_c", [_created_event()])
    assert session.auto_sync is None


def test_rehydrate_auto_sync_is_none_when_explicitly_null() -> None:
    """A session created with the field omitted persists ``auto_sync: null``; that
    replays as ``None`` (inherit), identical to the absent case."""
    session = rehydrate_from_events("sesn_d", [_created_event(auto_sync=None)])
    assert session.auto_sync is None


def test_auto_sync_round_trips_through_session_to_dict_and_back() -> None:
    """``to_dict``/``from_dict`` preserve the override, which is what the live
    session index and the log serialisation both depend on."""
    session = rehydrate_from_events("sesn_e", [_created_event(auto_sync=False)])
    payload = session.to_dict()
    assert payload["auto_sync"] is False
    assert type(session).from_dict(payload).auto_sync is False
