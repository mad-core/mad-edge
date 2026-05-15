"""Test-only AgentLauncher implementations.

Lives under tests/ on purpose: production code in src/ should not carry
fixtures or fakes. Each test that needs scripted agent output instantiates
ScriptedLauncher and feeds it a list of event sequences.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any


class RecordingLauncher:
    """AgentLauncher test double that records the prompt of every run
    and emits a single ``session.status_idle`` event per call.

    Used by tests that only care about *which prompts* the use case
    invokes the launcher with (e.g. issue #8 auto-sync verifies that
    a second post-run invocation receives the auto-sync prompt).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.session_ids: list[str] = []

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None:
        self.session_ids.append(session_id)
        self.calls.append(prompt)
        await emit("session.status_idle", {"stop_reason": "end_turn"})


class RaisingLauncher:
    """AgentLauncher test double that raises a fixed exception on every
    ``run`` call. Used by tests that exercise the dispatcher's
    launcher-failure path (``task.failed`` emission) without needing
    scripted bus events. Lives here per heuristic 3 so a contract
    drift on ``AgentLauncher.run`` fails one place, not many.
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None:
        raise self._exc


class ScriptedLauncher:
    """AgentLauncher test double. Each call to run() consumes the next
    scripted run from the queue and emits its events in order.
    """

    def __init__(self) -> None:
        self._queue: deque[list[dict]] = deque()
        self.calls: list[dict[str, Any]] = []

    def script(self, runs: list[list[dict]]) -> None:
        self._queue = deque(runs)

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None:
        self.calls.append({"session_id": session_id, "prompt": prompt, "workspace": workspace})
        if self._queue:
            events = self._queue.popleft()
        else:
            events = [{"type": "session.status_idle", "stop_reason": "end_turn"}]
        for event in events:
            await emit(event["type"], event)
