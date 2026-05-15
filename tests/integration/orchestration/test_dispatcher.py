"""Integration tests for ``Dispatcher``.

The dispatcher closes the orchestration loop: it subscribes to the
``EventBus``, applies events to the projection, and reacts to
``task.queued`` by invoking the existing launcher path. These tests
wire a real ``InMemoryEventBus`` + ``EventEmitter`` + projection +
``ScriptedLauncher`` and verify the four lifecycle paths from
ADR-0009: happy path dispatch, sequential single-dispatch across
sessions, launcher-exception → ``task.failed``, and orphan recovery
on restart.

State-based polling per heuristic 7 — no ``time.sleep + assert
count``. Every loop has a deadline + outcome assertion.
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

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from support.events import FakeEventStore
from support.launchers import ScriptedLauncher

_DEADLINE_S = 5.0


def _session(session_id: str, workspace: Path) -> Session:
    return Session(
        session_id=session_id,
        agent={"name": "test", "provider": "fake"},
        workspace=str(workspace),
        tokens_to_redact=[],
    )


def _scripted_two_runs(launcher: ScriptedLauncher) -> None:
    """A queued task triggers TWO launcher invocations: the primary run
    and the post-run auto-sync (issue #8). Each script must produce a
    terminal event so ``_run_launcher`` returns cleanly."""
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )


async def _wait_for_event_type(
    store: FakeEventStore, *, session_id: str, event_type: str, deadline: float = _DEADLINE_S
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


# -- Test fixture -------------------------------------------------------------


class _Harness:
    """Bundles the wired dispatcher, emitter, store, bus and a use case
    to enqueue tasks. Tests construct one and ``await harness.start()``
    before driving the system."""

    def __init__(self, sessions: dict[str, Session], launcher: ScriptedLauncher) -> None:
        self.store = FakeEventStore()
        self.bus = InMemoryEventBus()
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
        # The task lifecycle: queued -> dispatched -> (launcher events) -> completed.
        # ``_run_launcher`` adds session.status_running + 2x session.status_idle
        # because of the auto-sync (issue #8).
        assert types[0] == "task.queued"
        assert "task.dispatched" in types
        assert "task.completed" in types
        # task.dispatched must come before task.completed.
        assert types.index("task.dispatched") < types.index("task.completed")
        # And the task_id matches across the lifecycle.
        queued_call = next(c for c in h.store.calls if c[1] == "task.queued")
        dispatched_call = next(c for c in h.store.calls if c[1] == "task.dispatched")
        completed_call = next(c for c in h.store.calls if c[1] == "task.completed")
        assert queued_call[2]["task_id"] == str(output.task_id)
        assert dispatched_call[2]["task_id"] == str(output.task_id)
        assert completed_call[2]["task_id"] == str(output.task_id)

        # Launcher was invoked twice (primary + auto-sync).
        assert len(launcher.calls) == 2
        assert launcher.calls[0]["prompt"] == "hello"
    finally:
        await h.stop()


async def test_two_queued_tasks_run_sequentially_not_in_parallel(
    tmp_path: Path,
) -> None:
    """Single-dispatch invariant per ADR-0009 Decision 4: at most one
    task runs at a time, even on the same session."""
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

        # Wait for BOTH to complete.
        end = time.monotonic() + _DEADLINE_S
        while time.monotonic() < end:
            completed_ids = {c[2]["task_id"] for c in h.store.calls if c[1] == "task.completed"}
            if completed_ids == {str(a.task_id), str(b.task_id)}:
                break
            await asyncio.sleep(0.01)

        completed = [c for c in h.store.calls if c[1] == "task.completed"]
        assert [c[2]["task_id"] for c in completed] == [str(a.task_id), str(b.task_id)]

        # Single-dispatch invariant: B's task.dispatched comes after A's
        # task.completed.
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
    h = _Harness(sessions, launcher)  # type: ignore[arg-type]
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
        # And task.completed was NOT emitted.
        assert not any(c for c in h.store.calls if c[1] == "task.completed")
    finally:
        await h.stop()


async def test_orphan_dispatched_task_emits_task_failed_on_restart(
    tmp_path: Path,
) -> None:
    """ADR-0009 Decision 5: a task.dispatched without a matching terminal
    event in the bootstrapped projection means the prior process
    crashed mid-run. The dispatcher's start() emits
    `task.failed { reason: 'interrupted_by_restart' }` for each."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    h = _Harness(sessions, launcher)

    # Pre-populate the projection as if bootstrap_from_log had replayed
    # a [task.queued, task.dispatched] sequence. The orphan task is
    # in_flight with no terminal event.
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
