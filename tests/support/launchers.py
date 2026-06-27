"""Test-only AgentLauncher implementations.

Lives under tests/ on purpose: production code in src/ should not carry
fixtures or fakes. Each test that needs scripted agent output instantiates
ScriptedLauncher and feeds it a list of event sequences.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

# Sentinel type used by ScriptedLauncher.script_raising to distinguish
# error runs from normal event-list runs.
_ScriptEntry = tuple[list[dict], str | None] | BaseException


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
        self.models: list[str | None] = []
        self.efforts: list[str | None] = []
        self.conversation_ids: list[str | None] = []
        self.timeouts: list[float | None] = []

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None:
        self.session_ids.append(session_id)
        self.calls.append(prompt)
        self.models.append(model)
        self.efforts.append(effort)
        self.conversation_ids.append(conversation_id)
        self.timeouts.append(timeout_s)
        await emit("session.status_idle", {"stop_reason": "end_turn"})
        return None


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
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None:
        raise self._exc


class GatedLauncher:
    """AgentLauncher double whose every run blocks until ``release()`` is set.

    Used by workflow tests to freeze a step mid-run so the test can assert
    that a dependent step is held unqueued while its predecessor is still
    in flight (issue #90). Records the prompt of every run for ordering
    assertions; emits a single ``session.status_idle`` once released.
    """

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self.prompts: list[str] = []

    def release(self) -> None:
        self._gate.set()

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None:
        self.prompts.append(prompt)
        await self._gate.wait()
        await emit("session.status_idle", {"stop_reason": "end_turn"})
        return None


class ScriptedLauncher:
    """AgentLauncher test double. Each call to run() consumes the next
    scripted run from the queue and emits its events in order.

    The optional ``return_conversation_id`` constructor argument provides
    the conversation id that every run returns (default ``None``).
    Per-run overrides can be set via ``script_with_ids`` or
    ``script_raising`` (the latter supports exception-raising runs for
    rate-limit retry tests).
    """

    def __init__(self, return_conversation_id: str | None = None) -> None:
        self._queue: deque[list[dict]] = deque()
        self._ids: deque[str | None] = deque()
        self._exc_queue: deque[BaseException | None] = deque()
        self._default_id = return_conversation_id
        self.calls: list[dict[str, Any]] = []

    def script(self, runs: list[list[dict]]) -> None:
        self._queue = deque(runs)
        self._ids = deque()
        self._exc_queue = deque()

    def script_with_ids(self, runs: list[tuple[list[dict], str | None]]) -> None:
        """Script runs where each run may return a specific conversation id.

        ``runs`` is a list of ``(events, conversation_id)`` tuples.
        """
        self._queue = deque(r for r, _ in runs)
        self._ids = deque(cid for _, cid in runs)
        self._exc_queue = deque()

    def script_raising(self, entries: list[_ScriptEntry]) -> None:
        """Script runs that may raise exceptions (e.g. ``RateLimitError``).

        Each entry is either:
        - ``(events, conversation_id)`` — emit events then return the id.
        - A ``BaseException`` instance — raise it immediately (no events
          emitted), simulating a rate-limit or crash failure.

        Used by rate-limit retry tests so the dispatcher's retry loop
        can be exercised without a real subprocess.
        """
        self._queue = deque()
        self._ids = deque()
        self._exc_queue = deque()
        for entry in entries:
            if isinstance(entry, BaseException):
                self._queue.append([])
                self._ids.append(None)
                self._exc_queue.append(entry)
            else:
                events, cid = entry
                self._queue.append(events)
                self._ids.append(cid)
                self._exc_queue.append(None)

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
        model: str | None = None,
        effort: str | None = None,
        conversation_id: str | None = None,
        timeout_s: float | None = None,
    ) -> str | None:
        self.calls.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "workspace": workspace,
                "model": model,
                "effort": effort,
                "conversation_id": conversation_id,
                "timeout_s": timeout_s,
            }
        )
        # Check for a scripted exception before emitting any events.
        exc: BaseException | None = self._exc_queue.popleft() if self._exc_queue else None
        if exc is not None:
            if self._queue:
                self._queue.popleft()
            if self._ids:
                self._ids.popleft()
            raise exc

        if self._queue:
            events = self._queue.popleft()
        else:
            events = [{"type": "session.status_idle", "stop_reason": "end_turn"}]
        for event in events:
            await emit(event["type"], event)
        if self._ids:
            return self._ids.popleft()
        return self._default_id
