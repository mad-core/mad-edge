"""Unit tests for ``Dispatcher``.

The integration tests under ``tests/integration/orchestration/`` exercise
the dispatcher against the real ``InMemoryEventBus`` adapter; here we
swap in ``FakeEventBus`` (test double) so the dispatcher's branches
are covered by unit tests too. ``FakeEventBus`` buffers publishes that
arrive before a subscriber and drains them on subscribe, removing the
async-scheduling timing concerns so each test reads as a sequence of
states.

Coverage targets the four lifecycle paths from ADR-0009: happy-path
dispatch, single-dispatch invariant across two queued tasks, launcher
exception → ``task.failed``, and orphan recovery on restart.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC
from datetime import datetime as dt
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from support.events import FakeEventBus, FakeEventStore
from support.launchers import ScriptedLauncher

_DEADLINE_S = 2.0


def _session(session_id: str, workspace: Path) -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test", "provider": "fake"},
        workspace=str(workspace),
        tokens_to_redact=[],
    )


def _scripted_two_runs(launcher: ScriptedLauncher) -> None:
    """One queued task triggers two launcher invocations (primary + auto-sync)."""
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )


async def _wait_for_event_type(
    store: FakeEventStore,
    *,
    session_id: str,
    event_type: str,
    deadline: float = _DEADLINE_S,
) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if any(c for c in store.calls if c[0] == session_id and c[1] == event_type):
            return
        await asyncio.sleep(0.01)
    types = [c[1] for c in store.calls if c[0] == session_id]
    pytest.fail(f"timeout waiting for {event_type!r} on {session_id}; got {types}")


def _types_for_session(store: FakeEventStore, session_id: str) -> list[str]:
    return [c[1] for c in store.calls if c[0] == session_id]


class _Harness:
    """Wires a real ``EventEmitter`` + ``InMemoryTaskProjection`` over the
    ``FakeEventBus`` test double, plus an ``EnqueueTaskUseCase`` so tests
    can drive the system through the same surface production code uses."""

    def __init__(self, sessions: dict[str, Session], launcher: ScriptedLauncher) -> None:
        self.store = FakeEventStore()
        self.bus = FakeEventBus()
        self.projection = InMemoryTaskProjection()
        self.emitter = EventEmitter(store=self.store, bus=self.bus)
        self.sessions = sessions
        self.launcher_factory: Callable[[str], Any] = lambda _name: launcher
        self.dispatcher = Dispatcher(
            projection=self.projection,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=sessions,
            get_launcher=self.launcher_factory,
        )
        self.enqueue = EnqueueTaskUseCase(sessions_index=sessions, emitter=self.emitter)

    async def start(self) -> None:
        await self.dispatcher.start()

    async def stop(self) -> None:
        # Signal the active subscriber to stop iterating before cancelling
        # the loop task, to keep cleanup deterministic on the FakeEventBus.
        await self.bus.close_subscriber()
        await self.dispatcher.stop()


# -- Happy path ---------------------------------------------------------------


async def test_queued_task_dispatches_runs_and_completes(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        output = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hello"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        types = _types_for_session(h.store, "sesn_a")
        assert types[0] == "task.queued"
        assert "task.dispatched" in types
        assert "task.completed" in types
        assert types.index("task.dispatched") < types.index("task.completed")

        # Launcher invoked twice (primary + auto-sync).
        assert len(launcher.calls) == 2
        assert launcher.calls[0]["prompt"] == "hello"

        # The task_id is consistent across the lifecycle.
        queued = next(c for c in h.store.calls if c[1] == "task.queued")
        completed = next(c for c in h.store.calls if c[1] == "task.completed")
        assert queued[2]["task_id"] == str(output.task_id)
        assert completed[2]["task_id"] == str(output.task_id)
    finally:
        await h.stop()


async def test_two_queued_tasks_run_sequentially_not_in_parallel(
    tmp_path: Path,
) -> None:
    """Single-dispatch invariant per ADR-0009 Decision 4: at most one
    task dispatches at a time, even on the same session."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        a = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="A"))
        b = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="B"))

        end = time.monotonic() + _DEADLINE_S
        while time.monotonic() < end:
            completed_ids = {c[2]["task_id"] for c in h.store.calls if c[1] == "task.completed"}
            if completed_ids == {str(a.task_id), str(b.task_id)}:
                break
            await asyncio.sleep(0.01)

        completed = [c for c in h.store.calls if c[1] == "task.completed"]
        assert [c[2]["task_id"] for c in completed] == [str(a.task_id), str(b.task_id)]

        all_types = [(c[1], (c[2] or {}).get("task_id")) for c in h.store.calls]
        a_completed_idx = all_types.index(("task.completed", str(a.task_id)))
        b_dispatched_idx = all_types.index(("task.dispatched", str(b.task_id)))
        assert a_completed_idx < b_dispatched_idx
    finally:
        await h.stop()


# -- Negative twins -----------------------------------------------------------


async def test_launcher_exception_emits_task_failed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}

    class BoomLauncher:
        async def run(
            self,
            session_id: str,
            prompt: str,
            workspace: Path,
            emit: Callable[..., Any],
        ) -> None:
            raise RuntimeError("launcher boom")

    launcher = BoomLauncher()
    h = _Harness(sessions, ScriptedLauncher())  # placeholder
    h.launcher_factory = lambda _name: launcher
    h.dispatcher = Dispatcher(
        projection=h.projection,
        emitter=h.emitter,
        bus=h.bus,
        sessions_index=sessions,
        get_launcher=h.launcher_factory,
    )
    await h.start()
    try:
        output = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="will boom"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.failed")
        failed = next(c for c in h.store.calls if c[1] == "task.failed")
        assert failed[2]["task_id"] == str(output.task_id)
        assert "launcher boom" in failed[2]["reason"]
        # task.completed must NOT be emitted in the failure path.
        assert not any(c for c in h.store.calls if c[1] == "task.completed")
    finally:
        await h.stop()


async def test_orphan_dispatched_task_emits_task_failed_on_restart(
    tmp_path: Path,
) -> None:
    """ADR-0009 Decision 5: a task.dispatched without a matching terminal
    event in the bootstrapped projection means the prior process
    crashed mid-run. Dispatcher.start() emits ``task.failed`` with
    ``reason='interrupted_by_restart'`` for each."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    h = _Harness(sessions, launcher)

    orphan_id = uuid4()
    h.projection._in_flight["sesn_a"] = Task(
        task_id=orphan_id,
        session_id="sesn_a",
        content="lost work",
        scheduled_for="now",
        created_at=dt(2026, 5, 7, tzinfo=UTC),
    )

    await h.start()
    try:
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.failed")
        failed = next(c for c in h.store.calls if c[1] == "task.failed")
        assert failed[2]["task_id"] == str(orphan_id)
        assert failed[2]["reason"] == "interrupted_by_restart"
    finally:
        await h.stop()
