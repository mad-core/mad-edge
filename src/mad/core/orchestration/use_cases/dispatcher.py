"""Dispatcher — the orchestration loop that turns queued tasks into launcher runs.

ADR-0009 Decision 4 (single dispatch at a time) and Decision 5
(orphan detection on restart) are implemented here. The dispatcher is
a lifespan-managed asyncio task: ``start()`` is awaited at app startup,
``stop()`` at shutdown.

Design notes:

- **Single subscription, no filter.** The dispatcher subscribes to all
  events on the ``EventBus`` and forwards each to
  ``TaskProjection.apply`` so the projection stays current.
  Volume during a launcher run (potentially hundreds of ``agent.output``
  events) fits inside the bus's bounded queue because ``apply()`` is
  microseconds and the loop drains continuously.

- **Single in-flight, tracked locally.** The dispatcher records its
  own ``_in_flight`` ``(session_id, task_id)`` so it can decide
  whether to start the next task without racing the projection.
  Cross-session parallelism is deferred (ADR-0009 Consequences); v1 is
  serial across all sessions.

- **Completion via ``await``, not via bus.** ``_run_launcher`` (in
  ``mad.core.sessions.use_cases.send_user_message``) emits two
  ``session.status_idle`` events per dispatch — the primary run and
  the post-run auto-sync (issue #8). Reacting to the bus would require
  distinguishing the two; awaiting the launcher coroutine is
  unambiguous.

- **Tactical import of ``_run_launcher``.** The dispatcher imports the
  underscore-prefixed function from ``send_user_message``. A future
  refactor will hoist it to a shared use case once the patterns settle
  (third use site); doing it now would expand this PR's scope past the
  orchestration foundation. Documented as a known refactor candidate.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.events.ports.event_bus import EventBus, EventFilter
from mad.core.orchestration.domain.dispatch_policy import (
    can_dispatch,
    next_window_opening,
)
from mad.core.orchestration.domain.task import Task
from mad.core.orchestration.ports.clock import Clock
from mad.core.orchestration.ports.task_projection import TaskProjection
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.use_cases.send_user_message import _run_launcher

_DEFAULT_TICK_INTERVAL_S = 30.0


class Dispatcher:
    """Drains the orchestration queue by invoking the existing launcher path."""

    def __init__(
        self,
        projection: TaskProjection,
        emitter: EventEmitter,
        bus: EventBus,
        sessions_index: dict[str, Session],
        get_launcher: Callable[[str], Any],
        clock: Clock | None = None,
        tick_interval_s: float = _DEFAULT_TICK_INTERVAL_S,
    ) -> None:
        self._projection = projection
        self._emitter = emitter
        self._bus = bus
        self._sessions = sessions_index
        self._get_launcher = get_launcher
        self._clock = clock
        self._tick_interval_s = tick_interval_s

        self._loop_task: asyncio.Task[None] | None = None
        self._launch_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._in_flight: tuple[str, UUID] | None = None
        self._subscription: AsyncIterator[Event] | None = None
        # Track tasks that the bus loop has already accounted for via
        # task.queued_for_window so the periodic tick doesn't re-emit
        # them every time it sees the queue still has them.
        self._deferred_tasks: set[UUID] = set()

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Bootstrap orphan recovery and begin the dispatch loop.

        Caller MUST have already invoked ``projection.bootstrap_from_log``
        before this — otherwise the orphan check sees an empty
        projection and silently skips real orphans.

        Subscribes to the bus *synchronously* before returning so that any
        event published after ``start()`` returns is delivered to the loop —
        otherwise a publish that races ahead of the loop task scheduling
        would be lost.
        """
        await self._recover_orphans()
        self._subscription = self._bus.subscribe(EventFilter())
        self._loop_task = asyncio.create_task(self._loop())
        if self._clock is not None and self._tick_interval_s > 0:
            self._tick_task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        """Cancel the dispatch loop, the tick loop, and the in-flight launcher task."""
        for task in (self._launch_task, self._loop_task, self._tick_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._loop_task = None
        self._launch_task = None
        self._tick_task = None

    # -- Orphan recovery (ADR-0009 Decision 5) -----------------------------

    async def _recover_orphans(self) -> None:
        """Emit ``task.failed { reason: 'interrupted_by_restart' }`` for any
        task that is in-flight after the projection bootstrap.

        A ``task.dispatched`` without a terminal event means the previous
        process crashed mid-run. Emitting ``task.failed`` cleans the
        projection (via the loop's ``apply``) once the dispatch loop
        starts."""
        for session_id in list(self._sessions.keys()):
            orphan = self._projection.in_flight(session_id)
            if orphan is None:
                continue
            await self._emitter.emit(
                session_id,
                "task.failed",
                {
                    "task_id": str(orphan.task_id),
                    "reason": "interrupted_by_restart",
                },
            )

    # -- Main loop ---------------------------------------------------------

    async def _loop(self) -> None:
        # Try to dispatch immediately — bootstrap may have left queued items.
        await self._maybe_dispatch_next()
        assert self._subscription is not None  # set in start()
        async for event in self._subscription:
            self._projection.apply(event)
            if event.type == "task.queued":
                await self._on_task_queued(event)

    async def _on_task_queued(self, event: Event) -> None:
        """Handle a freshly enqueued task — dispatch immediately or defer."""
        session_id = event.session_id
        if session_id not in self._sessions:
            return
        if self._can_dispatch_for_session(session_id):
            await self._maybe_dispatch_next()
            return
        # Policy says no right now — emit task.queued_for_window so the
        # dashboard surface (issue 3) can show "Mad will run X tasks at
        # 18:00 tonight." Track the task_id so the periodic tick doesn't
        # re-emit on every cycle.
        task_id_str = event.data["task_id"]
        task_id = UUID(task_id_str)
        if task_id in self._deferred_tasks:
            return
        self._deferred_tasks.add(task_id)
        scheduled_for = self._next_window_opening_iso(session_id)
        await self._emitter.emit(
            session_id,
            "task.queued_for_window",
            {"task_id": task_id_str, "scheduled_for": scheduled_for},
        )

    async def _tick_loop(self) -> None:
        """Periodic policy evaluation — fires every ``tick_interval_s``."""
        try:
            while True:
                await asyncio.sleep(self._tick_interval_s)
                await self._maybe_dispatch_next()
        except asyncio.CancelledError:
            raise

    async def _maybe_dispatch_next(self) -> None:
        """Single-dispatch invariant — start at most one task across all sessions."""
        if self._in_flight is not None:
            return
        next_task = self._find_next_dispatchable()
        if next_task is None:
            return
        self._in_flight = (next_task.session_id, next_task.task_id)
        # Decrement the manual-drain counter if this dispatch was authorized
        # by an explicit POST /trigger.
        session = self._sessions[next_task.session_id]
        if session.manual_drain_remaining > 0:
            session.manual_drain_remaining -= 1
        # The task is leaving the queue — clear it from the deferred set so
        # if it ever returns to queued state (it can't in v1, but defensive)
        # the next deferral re-emits cleanly.
        self._deferred_tasks.discard(next_task.task_id)
        await self._emitter.emit(
            next_task.session_id,
            "task.dispatched",
            {"task_id": str(next_task.task_id)},
        )
        self._launch_task = asyncio.create_task(self._run_task(next_task))

    def _find_next_dispatchable(self) -> Task | None:
        for session_id in self._sessions:
            if not self._can_dispatch_for_session(session_id):
                continue
            queued = self._projection.queued(session_id)
            if queued:
                return queued[0]
        return None

    def _can_dispatch_for_session(self, session_id: str) -> bool:
        session = self._sessions[session_id]
        instant = self._clock.now() if self._clock is not None else None
        # When no clock is wired, fall back to immediate-only behavior so
        # legacy test setups (PR #29's _Harness without a clock) keep
        # working. Production always wires SystemClock.
        if instant is None:
            return type(session.dispatch_policy).__name__ == "ImmediatePolicy"
        return can_dispatch(
            session.dispatch_policy,
            instant,
            manual_drain_remaining=session.manual_drain_remaining,
        )

    def _next_window_opening_iso(self, session_id: str) -> str | None:
        session = self._sessions[session_id]
        if self._clock is None:
            return None
        opening = next_window_opening(session.dispatch_policy, self._clock.now())
        return opening.isoformat() if opening is not None else None

    async def _run_task(self, task: Task) -> None:
        """Drive the launcher for one task, then emit task.completed/failed."""
        try:
            session = self._sessions[task.session_id]
            await _run_launcher(
                session=session,
                session_id=task.session_id,
                prompt=task.content,
                get_launcher=self._get_launcher,
                emitter=self._emitter,
                propagate_failures=True,
            )
        except Exception as exc:
            await self._emitter.emit(
                task.session_id,
                "task.failed",
                {"task_id": str(task.task_id), "reason": str(exc)},
            )
        else:
            await self._emitter.emit(
                task.session_id,
                "task.completed",
                {"task_id": str(task.task_id)},
            )
        finally:
            self._in_flight = None
            self._launch_task = None
            await self._maybe_dispatch_next()
