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
from datetime import time as dtime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.dispatch_policy import (
    ManualPolicy,
    Window,
    WorkWindowPolicy,
)
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from support.clock import FakeClock
from support.events import FakeEventBus, FakeEventStore
from support.launchers import RaisingLauncher, ScriptedLauncher

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

    def __init__(
        self,
        sessions: dict[str, Session],
        launcher: ScriptedLauncher,
        *,
        clock: FakeClock | None = None,
        tick_interval_s: float = 0.05,
    ) -> None:
        self.store = FakeEventStore()
        self.bus = FakeEventBus()
        self.projection = InMemoryTaskProjection()
        self.emitter = EventEmitter(store=self.store, bus=self.bus)
        self.sessions = sessions
        self.clock = clock
        self.launcher_factory: Callable[[str], Any] = lambda _name: launcher
        self.dispatcher = Dispatcher(
            projection=self.projection,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=sessions,
            get_launcher=self.launcher_factory,
            clock=clock,
            tick_interval_s=tick_interval_s,
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

    launcher = RaisingLauncher(RuntimeError("launcher boom"))
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


# -- Dispatch policy gating (issue #33) ---------------------------------------

_MEX = ZoneInfo("America/Mexico_City")


def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> dt:
    return dt(year, month, day, hour, minute, tzinfo=_MEX)


async def _wait_for_event_absent(
    store: FakeEventStore,
    *,
    session_id: str,
    event_type: str,
    deadline: float = 0.3,
) -> None:
    """Poll for ``deadline`` seconds; pass iff the event NEVER appears.

    Used as a negative twin to ``_wait_for_event_type`` — proves a
    policy is actively suppressing dispatch instead of relying on a
    bare ``time.sleep`` then a single peek (heuristic 7)."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if any(c for c in store.calls if c[0] == session_id and c[1] == event_type):
            pytest.fail(f"unexpected {event_type!r} on {session_id}")
        await asyncio.sleep(0.01)


async def test_work_window_inside_window_dispatches_immediately(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = WorkWindowPolicy(
        windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_MEX),)
    )
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(_at(2026, 5, 9, 22, 0))  # inside the 18:00→08:00 window
    h = _Harness(sessions, launcher, clock=clock)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hi"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")
        # And no queued_for_window banner since dispatch was immediate.
        types = _types_for_session(h.store, "sesn_a")
        assert "task.queued_for_window" not in types
    finally:
        await h.stop()


async def test_work_window_outside_window_emits_queued_for_window_and_does_not_dispatch(
    tmp_path: Path,
) -> None:
    """The negative twin of the above: clock at midday → window is shut →
    dispatcher MUST NOT call the launcher and MUST emit
    ``task.queued_for_window`` so a UI can show the wait banner."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = WorkWindowPolicy(
        windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_MEX),)
    )
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(_at(2026, 5, 9, 12, 0))  # midday — window closed
    # Use a long tick so the periodic check doesn't fire mid-test.
    h = _Harness(sessions, launcher, clock=clock, tick_interval_s=10.0)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="defer me"))
        await _wait_for_event_type(
            h.store, session_id="sesn_a", event_type="task.queued_for_window"
        )
        await _wait_for_event_absent(h.store, session_id="sesn_a", event_type="task.dispatched")
        assert launcher.calls == []
        # The banner carries an ISO 'scheduled_for' that is at or after now.
        deferred = next(c for c in h.store.calls if c[1] == "task.queued_for_window")
        assert deferred[2]["scheduled_for"] is not None
    finally:
        await h.stop()


async def test_manual_policy_does_not_auto_dispatch(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = ManualPolicy()
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(_at(2026, 5, 9, 22, 0))
    h = _Harness(sessions, launcher, clock=clock, tick_interval_s=10.0)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hold"))
        await _wait_for_event_absent(h.store, session_id="sesn_a", event_type="task.dispatched")
        assert launcher.calls == []
    finally:
        await h.stop()


async def test_manual_drain_remaining_drains_exactly_n_then_stops(
    tmp_path: Path,
) -> None:
    """Setting ``manual_drain_remaining=2`` over 3 queued tasks dispatches
    the first two and leaves the third in the queue — proves the
    counter decrements per dispatch."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = ManualPolicy()
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    clock = FakeClock(_at(2026, 5, 9, 22, 0))
    h = _Harness(sessions, launcher, clock=clock, tick_interval_s=10.0)
    await h.start()
    try:
        a = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="A"))
        b = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="B"))
        c = await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="C"))
        # Operator triggers drain of 2.
        session.manual_drain_remaining = 2
        # Nudge the dispatcher into evaluating.
        await h.dispatcher._maybe_dispatch_next()

        # Wait for both A and B to complete.
        end = time.monotonic() + _DEADLINE_S
        while time.monotonic() < end:
            completed_ids = {c[2]["task_id"] for c in h.store.calls if c[1] == "task.completed"}
            if completed_ids >= {str(a.task_id), str(b.task_id)}:
                break
            await asyncio.sleep(0.01)

        completed = [c[2]["task_id"] for c in h.store.calls if c[1] == "task.completed"]
        assert str(a.task_id) in completed
        assert str(b.task_id) in completed
        # C MUST NOT have dispatched.
        dispatched_ids = {c[2]["task_id"] for c in h.store.calls if c[1] == "task.dispatched"}
        assert str(c.task_id) not in dispatched_ids
        # And the counter has returned to zero.
        assert session.manual_drain_remaining == 0
    finally:
        await h.stop()


async def test_periodic_tick_dispatches_when_window_opens(tmp_path: Path) -> None:
    """Schedule a task while the window is closed, then advance the
    clock past the opening boundary — the next tick MUST notice the
    policy is now open and dispatch.

    We use a short ``tick_interval_s=0.05`` for the test budget; the
    production default is 30s."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = WorkWindowPolicy(
        windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_MEX),)
    )
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(_at(2026, 5, 9, 12, 0))  # midday: closed
    h = _Harness(sessions, launcher, clock=clock, tick_interval_s=0.05)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="open at 18"))
        await _wait_for_event_type(
            h.store, session_id="sesn_a", event_type="task.queued_for_window"
        )
        # Now move clock past the window's opening — the next tick fires.
        clock.set(_at(2026, 5, 9, 18, 30))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")
        assert launcher.calls != []
    finally:
        await h.stop()


async def test_queued_for_window_emitted_only_once_across_repeated_ticks(
    tmp_path: Path,
) -> None:
    """The banner is "Mad will fire later" — emitting it on every tick
    would spam the JSONL log with hundreds of duplicates per overnight
    wait. Defended by ``Dispatcher._deferred_tasks``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.dispatch_policy = WorkWindowPolicy(
        windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_MEX),)
    )
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(_at(2026, 5, 9, 12, 0))
    h = _Harness(sessions, launcher, clock=clock, tick_interval_s=0.02)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="banner once"))
        await _wait_for_event_type(
            h.store, session_id="sesn_a", event_type="task.queued_for_window"
        )
        # Poll the invariant on every iteration (heuristic 7) so a
        # duplicate banner fails the test the moment it lands instead
        # of after a fixed sleep window. ``tick_interval_s=0.02`` means
        # the dispatcher fires the periodic loop ~10 times within
        # this 0.2s budget.
        end = time.monotonic() + 0.2
        while time.monotonic() < end:
            banners = [c for c in h.store.calls if c[1] == "task.queued_for_window"]
            assert len(banners) == 1, banners
            await asyncio.sleep(0.01)
    finally:
        await h.stop()
