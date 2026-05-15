"""Test-only doubles for the orchestration module.

Lives under ``tests/`` per ADR-0003 / testing-heuristic 3. Use cases
inject these to verify orchestration logic without spinning up the
real projection or replaying a JSONL log.
"""

from __future__ import annotations

from mad.core.orchestration.domain.task import Task


class FakeTaskQueue:
    """In-memory ``TaskQueue`` double.

    Tests script per-session ``queued`` and ``in_flight`` state. The
    projection's actual replay logic is exercised in the integration
    suite (Phase 4).
    """

    def __init__(
        self,
        queued: dict[str, list[Task]] | None = None,
        in_flight: dict[str, Task] | None = None,
    ) -> None:
        self._queued: dict[str, list[Task]] = {
            sid: list(tasks) for sid, tasks in (queued or {}).items()
        }
        self._in_flight: dict[str, Task] = dict(in_flight or {})

    def queued(self, session_id: str) -> list[Task]:
        return list(self._queued.get(session_id, []))

    def in_flight(self, session_id: str) -> Task | None:
        return self._in_flight.get(session_id)
