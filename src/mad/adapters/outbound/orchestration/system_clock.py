"""``SystemClock`` — production ``Clock`` implementation.

Returns ``datetime.now(UTC)``. UTC-anchored so the dispatch-policy
window evaluator (next issue) can compare against an IANA-zoned window
without timezone-mismatch surprises.
"""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Wall-clock source backed by ``datetime.now(UTC)``."""

    def now(self) -> datetime:
        return datetime.now(UTC)
