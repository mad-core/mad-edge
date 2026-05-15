"""Dispatch policies (issue #33 / ADR-0009 §9).

Three policies govern how queued tasks reach the launcher:

- ``ImmediatePolicy`` — default; dispatch on bus event without policy
  evaluation. Equivalent to PR #29's behavior.
- ``WorkWindowPolicy`` — dispatch only when the wall clock falls inside
  one of the configured ``Window`` instances (HH:MM start/end + IANA
  timezone + optional weekday filter). Windows can wrap midnight.
- ``ManualPolicy`` — queue accumulates indefinitely; only an explicit
  trigger drains the queue.

The HTTP layer (``PATCH /v1/sessions/{id}/dispatch_policy``) accepts a
discriminated-union shape and converts it into one of these value
objects via ``policy_from_dict``. The dispatcher reads
``Session.dispatch_policy`` and consults ``can_dispatch(policy, now)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import StrEnum
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Weekday(StrEnum):
    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"

    @classmethod
    def from_iso_index(cls, idx: int) -> Weekday:
        """Map ``datetime.isoweekday()`` (1=Mon..7=Sun) to a Weekday."""
        return _ISO_TO_WEEKDAY[idx]


_ISO_TO_WEEKDAY: dict[int, Weekday] = {
    1: Weekday.MON,
    2: Weekday.TUE,
    3: Weekday.WED,
    4: Weekday.THU,
    5: Weekday.FRI,
    6: Weekday.SAT,
    7: Weekday.SUN,
}

_ALL_DAYS: frozenset[Weekday] = frozenset(Weekday)


@dataclass(frozen=True)
class Window:
    """One contiguous (possibly midnight-wrapping) recurring time window.

    ``start`` and ``end`` are wall-clock times in ``timezone``. If
    ``end <= start`` the window wraps midnight (e.g. ``18:00`` →
    ``08:00`` is "evening through next morning"). ``days`` defaults to
    every day; restrict to a subset (e.g. ``mon..fri``) for weekday-only
    schedules.
    """

    start: time
    end: time
    timezone: ZoneInfo
    days: frozenset[Weekday] = field(default_factory=lambda: _ALL_DAYS)

    def contains(self, instant: datetime) -> bool:
        """Return True iff ``instant`` falls inside this window in its tz.

        ``instant`` MUST be timezone-aware (UTC or any other zone). It is
        converted to ``self.timezone`` before comparison so DST shifts are
        handled by ``zoneinfo``.
        """
        if instant.tzinfo is None:
            raise ValueError("Window.contains requires a timezone-aware datetime")
        local = instant.astimezone(self.timezone)
        weekday = Weekday.from_iso_index(local.isoweekday())
        # Strict reading: weekday-restricted windows ONLY match the
        # configured weekdays. Cross-midnight is a wall-clock concern,
        # not a calendar-day concern. Users wanting "fri 22:00 → sat
        # 06:00" must list both ``fri`` and ``sat``.
        if weekday not in self.days and not (
            self._wraps_midnight()
            and Weekday.from_iso_index(local.isoweekday() % 7 + 1) in self.days
        ):
            return False
        local_t = local.timetz().replace(tzinfo=None)
        if self._wraps_midnight():
            return local_t >= self.start or local_t < self.end
        return self.start <= local_t < self.end

    def _wraps_midnight(self) -> bool:
        return self.end <= self.start


@dataclass(frozen=True)
class ImmediatePolicy:
    """Default — dispatch as soon as the queue has work."""

    kind: Literal["immediate"] = "immediate"


@dataclass(frozen=True)
class WorkWindowPolicy:
    """Dispatch only when the clock is inside one of ``windows``."""

    windows: tuple[Window, ...]
    kind: Literal["work_window"] = "work_window"

    def is_open(self, instant: datetime) -> bool:
        return any(w.contains(instant) for w in self.windows)


@dataclass(frozen=True)
class ManualPolicy:
    """Queue accumulates; only ``POST /trigger`` drains it."""

    kind: Literal["manual"] = "manual"


DispatchPolicy = ImmediatePolicy | WorkWindowPolicy | ManualPolicy


def policy_from_dict(payload: dict[str, Any]) -> DispatchPolicy:
    """Convert an HTTP-layer dict into a domain ``DispatchPolicy``.

    Raises ``InvalidDispatchPolicy`` (a ValueError) on malformed input.
    The HTTP route maps that to a 422 via the existing handler.
    """
    kind = payload.get("kind")
    if kind == "immediate":
        return ImmediatePolicy()
    if kind == "manual":
        return ManualPolicy()
    if kind == "work_window":
        windows_raw = payload.get("windows") or []
        if not windows_raw:
            raise InvalidDispatchPolicy("work_window policy requires at least one window")
        windows = tuple(_window_from_dict(w) for w in windows_raw)
        return WorkWindowPolicy(windows=windows)
    raise InvalidDispatchPolicy(f"unknown dispatch policy kind: {kind!r}")


def policy_to_dict(policy: DispatchPolicy) -> dict[str, Any]:
    """Serialize a ``DispatchPolicy`` for events and HTTP responses."""
    if isinstance(policy, ImmediatePolicy):
        return {"kind": "immediate"}
    if isinstance(policy, ManualPolicy):
        return {"kind": "manual"}
    return {
        "kind": "work_window",
        "windows": [
            {
                "start": w.start.strftime("%H:%M"),
                "end": w.end.strftime("%H:%M"),
                "timezone": str(w.timezone),
                "days": sorted(d.value for d in w.days),
            }
            for w in policy.windows
        ],
    }


def _window_from_dict(payload: dict[str, Any]) -> Window:
    try:
        start = _parse_hhmm(payload["start"])
        end = _parse_hhmm(payload["end"])
    except KeyError as exc:
        raise InvalidDispatchPolicy(f"window missing field: {exc.args[0]!r}") from exc
    tz_name = payload.get("timezone")
    if not tz_name:
        raise InvalidDispatchPolicy("window missing field: 'timezone'")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise InvalidDispatchPolicy(f"unknown IANA timezone: {tz_name!r}") from exc
    days_raw = payload.get("days")
    if days_raw is None:
        days = _ALL_DAYS
    else:
        try:
            days = frozenset(Weekday(d) for d in days_raw)
        except ValueError as exc:
            raise InvalidDispatchPolicy(f"unknown weekday in 'days': {exc}") from exc
        if not days:
            raise InvalidDispatchPolicy("window 'days' cannot be empty when provided")
    return Window(start=start, end=end, timezone=tz, days=days)


def _parse_hhmm(value: str) -> time:
    """Parse ``HH:MM`` (24h) into a ``datetime.time``."""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise InvalidDispatchPolicy(f"expected 'HH:MM' time literal, got {value!r}")
    try:
        hh = int(value[:2])
        mm = int(value[3:])
    except ValueError as exc:
        raise InvalidDispatchPolicy(f"expected 'HH:MM' time literal, got {value!r}") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise InvalidDispatchPolicy(f"out-of-range HH:MM: {value!r}")
    return time(hour=hh, minute=mm)


def can_dispatch(
    policy: DispatchPolicy,
    instant: datetime,
    *,
    manual_drain_remaining: int = 0,
) -> bool:
    """Return True iff this policy allows dispatch at ``instant``.

    ``manual_drain_remaining`` lets a ``ManualPolicy`` session opt into
    transient dispatch after ``POST /trigger`` — see ADR-0009 §9.
    """
    if isinstance(policy, ImmediatePolicy):
        return True
    if isinstance(policy, ManualPolicy):
        return manual_drain_remaining > 0
    return policy.is_open(instant)


def next_window_opening(
    policy: DispatchPolicy,
    instant: datetime,
    *,
    horizon_days: int = 14,
) -> datetime | None:
    """Return the next datetime (UTC) at which a ``WorkWindowPolicy``
    becomes dispatchable. ``None`` for ``ImmediatePolicy`` (always open)
    and ``ManualPolicy`` (never opens autonomously).

    Walks forward in 1-minute increments up to ``horizon_days`` (default
    14) — good enough for "what's the next overnight window?" semantics
    without committing to closed-form interval math. Returns ``None`` if
    no window opens within the horizon.
    """
    if not isinstance(policy, WorkWindowPolicy):
        return None
    from datetime import timedelta

    cursor = instant
    end = instant + timedelta(days=horizon_days)
    step = timedelta(minutes=1)
    while cursor < end:
        if policy.is_open(cursor):
            return cursor
        cursor += step
    return None


class InvalidDispatchPolicy(ValueError):
    """Raised by ``policy_from_dict`` for any malformed input.

    Inherits from ``ValueError`` so the existing app-level
    ``ValueError`` handler maps it to 422 without a new handler.
    """
