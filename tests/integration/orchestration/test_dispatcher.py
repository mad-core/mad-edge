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
from support.launchers import RaisingLauncher, ScriptedLauncher

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
        # Auto-sync is off by default (issue #109), so ``_run_launcher`` adds
        # session.status_running + a single session.status_idle (no second run).
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

        # Auto-sync is off by default (issue #109), so the launcher runs once.
        assert len(launcher.calls) == 1
        assert launcher.calls[0]["prompt"] == "hello"
    finally:
        await h.stop()


async def test_dispatch_threads_per_session_timeout_to_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatcher resolves a session's timeout_s override and threads it
    into the launcher run (issue #61) — the shared _run_launcher path."""
    monkeypatch.delenv("MAD_AGENT_TIMEOUT_S", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.timeout_s = 17.0
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hello"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")
        assert launcher.calls[0]["timeout_s"] == 17.0
    finally:
        await h.stop()


async def test_dispatch_uses_default_timeout_when_session_has_no_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a session without timeout_s and no env var dispatches
    with the 600 s default."""
    monkeypatch.delenv("MAD_AGENT_TIMEOUT_S", raising=False)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hello"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")
        assert launcher.calls[0]["timeout_s"] == 600.0
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

    launcher = RaisingLauncher(RuntimeError("launcher boom"))
    h = _Harness(sessions, launcher)
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


# -- Cross-session priority (issue #46) ----------------------------------------


async def test_higher_priority_session_dispatches_before_lower_priority(
    tmp_path: Path,
) -> None:
    """Cross-session order is (-priority, head arrived_at): the priority-5
    session's task dispatches first even though the priority-1 session
    (a) sits first in the index and (b) holds the earlier-arrived task.
    The lower-priority task still runs — strictly after."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    low = _session("sesn_low", workspace)
    high = _session("sesn_high", workspace)
    high.priority = 5
    sessions = {"sesn_low": low, "sesn_high": high}
    launcher = ScriptedLauncher()  # unscripted runs self-complete with status_idle
    h = _Harness(sessions, launcher)

    # Pre-populate the projection as if bootstrap replayed two queued
    # tasks; the dispatcher's first _maybe_dispatch_next picks from here.
    low_task = Task(
        task_id=uuid4(),
        session_id="sesn_low",
        content="arrived first",
        scheduled_for="now",
        created_at=dt(2026, 6, 1, 12, 0, tzinfo=UTC),
    )
    high_task = Task(
        task_id=uuid4(),
        session_id="sesn_high",
        content="arrived later",
        scheduled_for="now",
        created_at=dt(2026, 6, 1, 12, 30, tzinfo=UTC),
    )
    h.projection._queued["sesn_low"] = [low_task]
    h.projection._queued["sesn_high"] = [high_task]

    await h.start()
    try:
        await _wait_for_event_type(h.store, session_id="sesn_low", event_type="task.completed")

        dispatched = [c[2]["task_id"] for c in h.store.calls if c[1] == "task.dispatched"]
        assert dispatched == [str(high_task.task_id), str(low_task.task_id)]
    finally:
        await h.stop()


async def test_orphan_recovery_clears_projection_in_flight(tmp_path: Path) -> None:
    """The orphan ``task.failed`` is emitted BEFORE the bus subscription
    starts, so recovery must clear the projection itself — otherwise
    GET /v1/queue shows a phantom in-flight forever."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    h = _Harness(sessions, ScriptedLauncher())
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
        assert h.projection.in_flight("sesn_a") is None
        assert h.projection.pending_session_ids() == []
    finally:
        await h.stop()


# -- Post-run auto-sync gate (issue #109) --------------------------------------
#
# The dispatcher resolves the gate as task > session > MAD_AUTO_SYNC > False (off
# by default, opt-in). The task level is the one that fixes the bug: a queued job
# that manages its own named branch/PR sets ``auto_sync=False``, and the post-run
# publish run — which cannot see that branch and would open a duplicate PR on
# ``mad/<session_id>`` — never starts. The observable contract is the launcher
# CALL COUNT: exactly one invocation (the primary) instead of two.
#
# ``test_task_auto_sync_true_wins_over_session_auto_sync_false`` below is the
# positive twin: an explicit opt-in fires the second (auto-sync) run, so the
# launcher is invoked twice. ``test_queued_task_dispatches_runs_and_completes``
# above shows the default: with no override anywhere the launcher runs once.


def _scripted_one_run(launcher: ScriptedLauncher) -> None:
    """Script a single run — the gated case, where auto-sync never fires."""
    launcher.script([[{"type": "session.status_idle", "stop_reason": "end_turn"}]])


async def test_task_auto_sync_false_skips_the_post_run_sync_run(tmp_path: Path) -> None:
    """A task with ``auto_sync=False`` invokes the launcher exactly once and
    records ``agent.autosync.skipped`` — no second run, so no duplicate PR."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    _scripted_one_run(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_a", content="work on my own branch", auto_sync=False)
        )
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert len(launcher.calls) == 1, (
            "auto_sync=False must suppress the post-run auto-sync run entirely; "
            f"launcher prompts were {[c['prompt'] for c in launcher.calls]}"
        )
        assert launcher.calls[0]["prompt"] == "work on my own branch"

        types = _types_for_session(h.store, "sesn_a")
        assert "agent.autosync.skipped" in types
        skipped = next(c for c in h.store.calls if c[1] == "agent.autosync.skipped")
        assert skipped[2]["reason"] == "disabled"
        # The skip is non-terminal: the task still completes normally.
        assert "task.completed" in types
    finally:
        await h.stop()


async def test_task_auto_sync_false_wins_over_session_auto_sync_true(tmp_path: Path) -> None:
    """Precedence: the per-task opt-out beats a session that leaves auto-sync on.

    This is the exact shape of issue #109 — a long-lived session with the safety
    net on, plus one task that owns its branch and PR.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.auto_sync = True
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_one_run(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_a", content="hello", auto_sync=False)
        )
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert len(launcher.calls) == 1
        assert "agent.autosync.skipped" in _types_for_session(h.store, "sesn_a")
    finally:
        await h.stop()


async def test_task_auto_sync_true_wins_over_session_auto_sync_false(tmp_path: Path) -> None:
    """Negative twin: the per-task opt-IN beats a session that opted out — the
    launcher runs twice and nothing is skipped."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.auto_sync = False
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_a", content="hello", auto_sync=True)
        )
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert len(launcher.calls) == 2
        assert "auto-sync" in launcher.calls[1]["prompt"].lower()
        assert "agent.autosync.skipped" not in _types_for_session(h.store, "sesn_a")
    finally:
        await h.stop()


async def test_session_auto_sync_false_applies_when_task_leaves_it_unset(
    tmp_path: Path,
) -> None:
    """With no per-task override, the session's opt-out governs the dispatch."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = _session("sesn_a", workspace)
    session.auto_sync = False
    sessions = {"sesn_a": session}
    launcher = ScriptedLauncher()
    _scripted_one_run(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hello"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert len(launcher.calls) == 1
        assert "agent.autosync.skipped" in _types_for_session(h.store, "sesn_a")
    finally:
        await h.stop()


async def test_env_auto_sync_false_applies_when_task_and_session_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator env default is the last level before the hard-coded True, and
    it reaches the dispatcher's launcher path."""
    monkeypatch.setenv("MAD_AUTO_SYNC", "false")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    sessions = {"sesn_a": _session("sesn_a", workspace)}
    launcher = ScriptedLauncher()
    _scripted_one_run(launcher)
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="hello"))
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert len(launcher.calls) == 1
        assert "agent.autosync.skipped" in _types_for_session(h.store, "sesn_a")
    finally:
        await h.stop()
