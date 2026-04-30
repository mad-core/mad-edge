from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Any, Callable, Coroutine


class FakeLauncher:
    def __init__(self) -> None:
        self._queue: deque[list[dict]] = deque()

    def script(self, runs: list[list[dict]]) -> None:
        self._queue = deque(runs)

    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None:
        if self._queue:
            events = self._queue.popleft()
        else:
            events = [{"type": "session.status_idle", "stop_reason": "end_turn"}]
        for event in events:
            event_type = event["type"]
            await emit(event_type, event)
