"""Clock port — abstract time source for the orchestration module.

Introduced in v1 (ADR-0009 Decision 8) even though the dispatcher has
no time-based behaviour yet. The next issue (dispatch policies —
``work_window`` + manual) needs ``now()`` to evaluate window
predicates; introducing the port now means that issue is purely
additive (a new use case input) rather than a constructor-signature
retrofit.

The production implementation is ``SystemClock`` at
``mad.adapters.outbound.orchestration.system_clock``. Tests inject a
fake clock when scheduling behaviour is exercised.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Abstract source of the current wall-clock time."""

    def now(self) -> datetime:
        """Return a timezone-aware ``datetime`` representing 'now'."""
        ...
