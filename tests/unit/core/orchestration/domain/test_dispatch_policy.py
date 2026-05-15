"""Unit tests for dispatch policies (issue #33 / ADR-0009 §9).

Pure-domain tests over the policy value objects, ``Window.contains``,
``can_dispatch``, ``policy_from_dict`` / ``policy_to_dict``, and the
``next_window_opening`` helper. No I/O, no asyncio — every test is
microsecond-fast.

DST handling is tested explicitly: the spring-forward gap (where 02:00
does not exist locally) and the fall-back overlap (where 02:00 happens
twice) MUST behave per ``zoneinfo`` semantics without losing scheduled
work — covered by ``test_window_contains_*_dst_*`` below.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from mad.core.orchestration.domain.dispatch_policy import (
    ImmediatePolicy,
    InvalidDispatchPolicy,
    ManualPolicy,
    Weekday,
    Window,
    WorkWindowPolicy,
    can_dispatch,
    next_window_opening,
    policy_from_dict,
    policy_to_dict,
)

_MEX = ZoneInfo("America/Mexico_City")
_NYC = ZoneInfo("America/New_York")


# -- Window.contains ----------------------------------------------------------


def test_window_contains_inside_simple_window() -> None:
    w = Window(start=time(9, 0), end=time(17, 0), timezone=_MEX)
    inside = datetime(2026, 5, 9, 12, 0, tzinfo=_MEX)
    assert w.contains(inside)


def test_window_contains_outside_simple_window() -> None:
    w = Window(start=time(9, 0), end=time(17, 0), timezone=_MEX)
    outside_morning = datetime(2026, 5, 9, 8, 59, tzinfo=_MEX)
    outside_evening = datetime(2026, 5, 9, 17, 0, tzinfo=_MEX)
    assert not w.contains(outside_morning)
    assert not w.contains(outside_evening)


def test_window_contains_wrap_midnight_evening_through_morning() -> None:
    """18:00 → 08:00 is the canonical 'overnight' shape."""
    w = Window(start=time(18, 0), end=time(8, 0), timezone=_MEX)
    assert w.contains(datetime(2026, 5, 9, 22, 0, tzinfo=_MEX))  # late evening
    assert w.contains(datetime(2026, 5, 10, 3, 0, tzinfo=_MEX))  # early morning
    assert not w.contains(datetime(2026, 5, 9, 12, 0, tzinfo=_MEX))  # midday


def test_window_contains_requires_aware_datetime() -> None:
    w = Window(start=time(9, 0), end=time(17, 0), timezone=_MEX)
    naive = datetime(2026, 5, 9, 12, 0)
    with pytest.raises(ValueError):
        w.contains(naive)


def test_window_contains_converts_utc_to_local_before_comparing() -> None:
    """UTC instant is interpreted in the window's IANA tz."""
    # America/Mexico_City is UTC-6 (CST, no DST since 2022). 18:00 local = 00:00 UTC next day.
    w = Window(start=time(18, 0), end=time(8, 0), timezone=_MEX)
    utc_instant = datetime(2026, 5, 10, 0, 0, tzinfo=UTC)  # = 2026-05-09 18:00 in MEX
    assert w.contains(utc_instant)


def test_window_contains_respects_weekday_filter() -> None:
    weekdays_only = frozenset({Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI})
    w = Window(start=time(9, 0), end=time(17, 0), timezone=_MEX, days=weekdays_only)
    # 2026-05-09 is a Saturday in any reasonable calendar — verify.
    saturday = datetime(2026, 5, 9, 12, 0, tzinfo=_MEX)
    assert saturday.isoweekday() == 6  # sanity
    assert not w.contains(saturday)
    # 2026-05-11 is a Monday.
    monday = datetime(2026, 5, 11, 12, 0, tzinfo=_MEX)
    assert monday.isoweekday() == 1
    assert w.contains(monday)


def test_window_contains_dst_spring_forward_skipped_hour() -> None:
    """In NYC on 2026-03-08, 02:00 → 03:00 is skipped (clocks jump
    forward). A window 01:30 → 02:30 effectively only fires for the
    01:30-02:00 half on that date — the missing hour can't be matched.
    The window does NOT misfire as 'open all day'."""
    w = Window(start=time(1, 30), end=time(2, 30), timezone=_NYC)
    # Just before the gap: 2026-03-08 01:45 NYC — open.
    inside = datetime(2026, 3, 8, 1, 45, tzinfo=_NYC)
    assert w.contains(inside)
    # 2026-03-08 03:30 NYC — outside (window has already closed).
    after_gap = datetime(2026, 3, 8, 3, 30, tzinfo=_NYC)
    assert not w.contains(after_gap)


def test_window_contains_dst_fall_back_overlap_window_open_both_passes() -> None:
    """In NYC on 2026-11-01, 01:00 → 02:00 happens twice. A window
    spanning that hour is open during BOTH instants — `zoneinfo`
    represents the second pass as the same wall-clock time. Mad's
    behavior: if the wall clock is inside the window, it's open."""
    w = Window(start=time(0, 30), end=time(2, 30), timezone=_NYC)
    first_pass = datetime(2026, 11, 1, 1, 30, tzinfo=_NYC, fold=0)  # before fall back
    second_pass = datetime(2026, 11, 1, 1, 30, tzinfo=_NYC, fold=1)  # after fall back
    assert w.contains(first_pass)
    assert w.contains(second_pass)


# -- can_dispatch -------------------------------------------------------------


def test_can_dispatch_immediate_always_true() -> None:
    instant = datetime(2026, 5, 9, 3, 0, tzinfo=UTC)
    assert can_dispatch(ImmediatePolicy(), instant)


def test_can_dispatch_manual_false_without_drain_pending() -> None:
    instant = datetime(2026, 5, 9, 3, 0, tzinfo=UTC)
    assert not can_dispatch(ManualPolicy(), instant)
    assert not can_dispatch(ManualPolicy(), instant, manual_drain_remaining=0)


def test_can_dispatch_manual_true_when_drain_pending() -> None:
    instant = datetime(2026, 5, 9, 3, 0, tzinfo=UTC)
    assert can_dispatch(ManualPolicy(), instant, manual_drain_remaining=3)
    assert can_dispatch(ManualPolicy(), instant, manual_drain_remaining=1)


def test_can_dispatch_work_window_inside_window_true() -> None:
    policy = WorkWindowPolicy(windows=(Window(start=time(18, 0), end=time(8, 0), timezone=_MEX),))
    inside = datetime(2026, 5, 9, 22, 0, tzinfo=_MEX)
    assert can_dispatch(policy, inside)


def test_can_dispatch_work_window_outside_window_false() -> None:
    policy = WorkWindowPolicy(windows=(Window(start=time(18, 0), end=time(8, 0), timezone=_MEX),))
    outside = datetime(2026, 5, 9, 12, 0, tzinfo=_MEX)
    assert not can_dispatch(policy, outside)


# -- policy_from_dict / policy_to_dict ----------------------------------------


def test_policy_from_dict_immediate() -> None:
    p = policy_from_dict({"kind": "immediate"})
    assert isinstance(p, ImmediatePolicy)


def test_policy_from_dict_manual() -> None:
    p = policy_from_dict({"kind": "manual"})
    assert isinstance(p, ManualPolicy)


def test_policy_from_dict_work_window_full_shape() -> None:
    p = policy_from_dict(
        {
            "kind": "work_window",
            "windows": [
                {
                    "start": "18:00",
                    "end": "08:00",
                    "timezone": "America/Mexico_City",
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                }
            ],
        }
    )
    assert isinstance(p, WorkWindowPolicy)
    assert len(p.windows) == 1
    w = p.windows[0]
    assert w.start == time(18, 0)
    assert w.end == time(8, 0)
    assert str(w.timezone) == "America/Mexico_City"
    assert w.days == frozenset({Weekday.MON, Weekday.TUE, Weekday.WED, Weekday.THU, Weekday.FRI})


def test_policy_from_dict_rejects_unknown_kind() -> None:
    with pytest.raises(InvalidDispatchPolicy):
        policy_from_dict({"kind": "schedule"})


def test_policy_from_dict_rejects_work_window_without_windows() -> None:
    with pytest.raises(InvalidDispatchPolicy):
        policy_from_dict({"kind": "work_window", "windows": []})


def test_policy_from_dict_rejects_unknown_timezone() -> None:
    with pytest.raises(InvalidDispatchPolicy):
        policy_from_dict(
            {
                "kind": "work_window",
                "windows": [{"start": "18:00", "end": "08:00", "timezone": "Atlantis/Capital"}],
            }
        )


def test_policy_from_dict_rejects_malformed_hhmm() -> None:
    with pytest.raises(InvalidDispatchPolicy):
        policy_from_dict(
            {
                "kind": "work_window",
                "windows": [{"start": "1800", "end": "08:00", "timezone": "America/Mexico_City"}],
            }
        )


def test_policy_from_dict_rejects_out_of_range_hhmm() -> None:
    with pytest.raises(InvalidDispatchPolicy):
        policy_from_dict(
            {
                "kind": "work_window",
                "windows": [{"start": "25:00", "end": "08:00", "timezone": "America/Mexico_City"}],
            }
        )


def test_policy_to_dict_round_trips_immediate_and_manual() -> None:
    assert policy_to_dict(ImmediatePolicy()) == {"kind": "immediate"}
    assert policy_to_dict(ManualPolicy()) == {"kind": "manual"}


def test_policy_to_dict_round_trips_work_window() -> None:
    p = WorkWindowPolicy(
        windows=(
            Window(
                start=time(18, 0),
                end=time(8, 0),
                timezone=_MEX,
                days=frozenset({Weekday.MON, Weekday.FRI}),
            ),
        )
    )
    out = policy_to_dict(p)
    assert out["kind"] == "work_window"
    assert out["windows"][0]["start"] == "18:00"
    assert out["windows"][0]["end"] == "08:00"
    assert out["windows"][0]["timezone"] == "America/Mexico_City"
    assert sorted(out["windows"][0]["days"]) == ["fri", "mon"]


# -- next_window_opening ------------------------------------------------------


def test_next_window_opening_returns_now_when_already_inside() -> None:
    p = WorkWindowPolicy(windows=(Window(start=time(18, 0), end=time(8, 0), timezone=_MEX),))
    inside = datetime(2026, 5, 9, 22, 0, tzinfo=_MEX)
    assert next_window_opening(p, inside) == inside


def test_next_window_opening_finds_evening_from_midday() -> None:
    p = WorkWindowPolicy(windows=(Window(start=time(18, 0), end=time(8, 0), timezone=_MEX),))
    midday = datetime(2026, 5, 9, 12, 0, tzinfo=_MEX)
    opening = next_window_opening(p, midday)
    assert opening is not None
    assert opening >= midday
    # Should be the same day at 18:00 MEX.
    assert opening.astimezone(_MEX).time() >= time(17, 59)
    assert opening.astimezone(_MEX).time() <= time(18, 1)


def test_next_window_opening_none_for_immediate_and_manual() -> None:
    instant = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    assert next_window_opening(ImmediatePolicy(), instant) is None
    assert next_window_opening(ManualPolicy(), instant) is None
