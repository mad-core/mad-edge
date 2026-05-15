"""Test-only ``Clock`` double for orchestration scheduling tests.

Lives under ``tests/`` per ADR-0003 / testing-heuristic 3. Tests inject
this so DST-boundary, window-open, and tick-cadence behavior is
deterministic — no ``time.sleep`` waiting for real wall-clock seconds
to pass (heuristic 7).

Only one method is required by the ``Clock`` Protocol (``now()``); the
``set`` / ``advance`` helpers exist for the test driver.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    """Manually-advanced ``Clock`` test double.

    The default starting instant is the UTC epoch +1 day (so leap-second
    edge cases at the actual epoch are out of the picture). Callers
    override via the constructor.
    """

    def __init__(self, instant: datetime | None = None) -> None:
        if instant is None:
            instant = datetime(1970, 1, 2, tzinfo=UTC)
        if instant.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware datetime")
        self._instant = instant

    def now(self) -> datetime:
        return self._instant

    def set(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FakeClock.set requires a timezone-aware datetime")
        self._instant = instant

    def advance(self, delta: timedelta) -> None:
        self._instant = self._instant + delta
