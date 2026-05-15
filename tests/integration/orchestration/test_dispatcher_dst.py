"""DST-boundary dispatcher tests (issue #33 acceptance criterion).

The hard part of WorkWindowPolicy is honoring the operator's intent
when the wall clock skips forward (spring-forward) or repeats
(fall-back). These tests pin Mad's behavior across both transitions
using ``FakeClock`` so they're deterministic — no waiting on real
DST cutovers.

The wired adapters are real (``InMemoryEventBus``, real ``Dispatcher``,
real ``EventEmitter``); only the clock and the launcher are doubled.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.dispatch_policy import (
    Window,
    WorkWindowPolicy,
)
from mad.core.orchestration.use_cases.dispatcher import Dispatcher
from mad.core.orchestration.use_cases.enqueue_task import (
    EnqueueTaskInput,
    EnqueueTaskUseCase,
)
from mad.core.sessions.domain.entities.session import Session
from support.clock import FakeClock
from support.events import FakeEventStore
from support.launchers import ScriptedLauncher

_DEADLINE_S = 5.0
_NYC = ZoneInfo("America/New_York")


def _session(workspace: Path, policy: WorkWindowPolicy) -> Session:
    s = Session(
        session_id="sesn_a",
        agent={"name": "test", "provider": "fake"},
        workspace=str(workspace),
        tokens_to_redact=[],
    )
    s.dispatch_policy = policy
    return s


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


async def _wait_for_event_absent(
    store: FakeEventStore,
    *,
    session_id: str,
    event_type: str,
    deadline: float = 0.3,
) -> None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if any(c for c in store.calls if c[0] == session_id and c[1] == event_type):
            pytest.fail(f"unexpected {event_type!r} on {session_id}")
        await asyncio.sleep(0.01)


def _build_dispatcher(
    sessions: dict[str, Session],
    launcher: ScriptedLauncher,
    clock: FakeClock,
    *,
    tick_interval_s: float = 0.05,
) -> tuple[Dispatcher, EventEmitter, FakeEventStore, InMemoryEventBus, EnqueueTaskUseCase]:
    store = FakeEventStore()
    bus = InMemoryEventBus()
    projection = InMemoryTaskProjection()
    emitter = EventEmitter(store=store, bus=bus)

    def factory(_name: str) -> Any:
        return launcher

    factory_typed: Callable[[str], Any] = factory
    dispatcher = Dispatcher(
        projection=projection,
        emitter=emitter,
        bus=bus,
        sessions_index=sessions,
        get_launcher=factory_typed,
        clock=clock,
        tick_interval_s=tick_interval_s,
    )
    enqueue = EnqueueTaskUseCase(sessions_index=sessions, emitter=emitter)
    return dispatcher, emitter, store, bus, enqueue


def _scripted_two_runs(launcher: ScriptedLauncher) -> None:
    launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )


# 2026-03-08 02:00 → 03:00 in America/New_York is the spring-forward gap.
# The wall clock skips from 01:59:59 EST directly to 03:00:00 EDT.
# A window 18:00 → 08:00 spans the gap; the operator's intent is
# "still open from 18:00 last night through 08:00 this morning."


async def test_window_spanning_spring_forward_remains_open_after_gap(
    tmp_path: Path,
) -> None:
    """At 04:00 NYC on the spring-forward date the wall clock is past
    the gap but well inside the 18:00→08:00 wrap-midnight window.
    Mad MUST treat the policy as OPEN and dispatch the queued task —
    operators don't expect Mad to "lose" their overnight runs because
    of a 1-hour DST jump."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = WorkWindowPolicy(windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_NYC),))
    sessions = {"sesn_a": _session(workspace, policy)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    # 04:00 NYC on 2026-03-08 — past the gap, before window closes at 08:00.
    clock = FakeClock(datetime(2026, 3, 8, 4, 0, tzinfo=_NYC))
    dispatcher, _, store, _, enqueue = _build_dispatcher(sessions, launcher, clock)

    await dispatcher.start()
    try:
        await enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="overnight"))
        await _wait_for_event_type(store, session_id="sesn_a", event_type="task.completed")
        # No banner — dispatch was immediate per the policy.
        types = [c[1] for c in store.calls if c[0] == "sesn_a"]
        assert "task.queued_for_window" not in types
    finally:
        await dispatcher.stop()


async def test_window_outside_after_spring_forward_does_not_dispatch(
    tmp_path: Path,
) -> None:
    """Negative twin: at 09:00 NYC on the spring-forward date the
    18:00→08:00 window is closed. Mad MUST NOT dispatch. This proves
    the previous test's pass wasn't an "always open" bug masquerading
    as a DST-correct success."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = WorkWindowPolicy(windows=(Window(start=dtime(18, 0), end=dtime(8, 0), timezone=_NYC),))
    sessions = {"sesn_a": _session(workspace, policy)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    # 09:00 NYC on 2026-03-08 — past the window's 08:00 close.
    clock = FakeClock(datetime(2026, 3, 8, 9, 0, tzinfo=_NYC))
    dispatcher, _, store, _, enqueue = _build_dispatcher(
        sessions, launcher, clock, tick_interval_s=10.0
    )

    await dispatcher.start()
    try:
        await enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="too late"))
        await _wait_for_event_type(store, session_id="sesn_a", event_type="task.queued_for_window")
        await _wait_for_event_absent(store, session_id="sesn_a", event_type="task.dispatched")
        assert launcher.calls == []
    finally:
        await dispatcher.stop()


# 2026-11-01 01:00 → 02:00 in America/New_York is the fall-back overlap.
# 01:30 EDT happens, then clocks roll back to 01:00 EST and 01:30 EST happens.


async def test_window_in_fall_back_overlap_dispatches_during_repeated_hour(
    tmp_path: Path,
) -> None:
    """The 01:00→02:00 hour fires twice on fall-back. A window covering
    that wall-clock band MUST be open during both passes — operators
    set the window in wall-clock terms, not UTC, so the second pass
    is part of the band they specified."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # A narrow window that sits squarely inside the doubled hour.
    policy = WorkWindowPolicy(
        windows=(Window(start=dtime(0, 30), end=dtime(2, 30), timezone=_NYC),)
    )
    sessions = {"sesn_a": _session(workspace, policy)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    # Second pass of 01:30 NYC on 2026-11-01 (after fall-back) — fold=1.
    clock = FakeClock(datetime(2026, 11, 1, 1, 30, tzinfo=_NYC, fold=1))
    dispatcher, _, store, _, enqueue = _build_dispatcher(sessions, launcher, clock)

    await dispatcher.start()
    try:
        await enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="repeat hour"))
        await _wait_for_event_type(store, session_id="sesn_a", event_type="task.completed")
    finally:
        await dispatcher.stop()


async def test_window_outside_fall_back_overlap_does_not_dispatch(
    tmp_path: Path,
) -> None:
    """Negative twin to ``test_window_in_fall_back_overlap_*``: at 03:30
    NYC on 2026-11-01 the wall clock is past the doubled hour AND past
    the window's 02:30 close. Mad MUST NOT dispatch — proves the
    previous test's pass isn't an "always open during fall-back" bug."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = WorkWindowPolicy(
        windows=(Window(start=dtime(0, 30), end=dtime(2, 30), timezone=_NYC),)
    )
    sessions = {"sesn_a": _session(workspace, policy)}
    launcher = ScriptedLauncher()
    _scripted_two_runs(launcher)
    clock = FakeClock(datetime(2026, 11, 1, 3, 30, tzinfo=_NYC))
    dispatcher, _, store, _, enqueue = _build_dispatcher(
        sessions, launcher, clock, tick_interval_s=10.0
    )

    await dispatcher.start()
    try:
        await enqueue.execute(EnqueueTaskInput(session_id="sesn_a", content="too late"))
        await _wait_for_event_type(store, session_id="sesn_a", event_type="task.queued_for_window")
        await _wait_for_event_absent(store, session_id="sesn_a", event_type="task.dispatched")
        assert launcher.calls == []
    finally:
        await dispatcher.stop()
