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
from mad.core.orchestration.domain.deployment_policy import (
    DeploymentDispatchPolicy,
    resolve_effective_policy,
)
from mad.core.orchestration.domain.dispatch_policy import (
    ImmediatePolicy,
    can_dispatch,
    next_window_opening,
)
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
from mad.core.orchestration.domain.model_config import (
    DeploymentModelConfig,
    resolve_effective_model,
)
from mad.core.orchestration.domain.ordering import order_ready_candidates
from mad.core.orchestration.domain.retry_schedule import backoff_s, exceeds_ceiling
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
        deployment_policy: DeploymentDispatchPolicy | None = None,
        deployment_model_config: DeploymentModelConfig | None = None,
    ) -> None:
        self._projection = projection
        self._emitter = emitter
        self._bus = bus
        self._sessions = sessions_index
        self._get_launcher = get_launcher
        self._clock = clock
        self._tick_interval_s = tick_interval_s
        # Process-global default that sessions without an override inherit
        # (issue #45). Held by reference so a live ``PUT /v1/dispatch_policy``
        # is observed on the next evaluation without restarting the loop.
        self._deployment_policy = deployment_policy or DeploymentDispatchPolicy()
        # Process-global model default (issue #55). None means omit --model.
        self._deployment_model_config = deployment_model_config

        self._loop_task: asyncio.Task[None] | None = None
        self._launch_task: asyncio.Task[None] | None = None
        self._tick_task: asyncio.Task[None] | None = None
        self._in_flight: tuple[str, UUID] | None = None
        self._stopping = False
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
        self._stopping = True
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
            event = await self._emitter.emit(
                session_id,
                "task.failed",
                {
                    "task_id": str(orphan.task_id),
                    "reason": "interrupted_by_restart",
                },
            )
            # The bus subscription only starts AFTER recovery (see
            # start()), so the loop never applies this event — apply it
            # here or the projection keeps a phantom in_flight for the
            # whole process lifetime and GET /v1/queue disagrees with
            # the dispatcher.
            self._projection.apply(event)

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
        if self._stopping:
            return
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
        """Pick the next task in cross-session dispatch order.

        Shares ``order_ready_candidates`` with ``GET /v1/queue`` (issue
        #46 Part D) so the operator-facing ``ready`` list and the
        dispatcher can never disagree: the queue view shows the list,
        the dispatcher takes ``[0]``.
        """
        eligible = [
            session
            for session_id, session in self._sessions.items()
            if self._can_dispatch_for_session(session_id)
        ]
        ordered = order_ready_candidates(eligible, self._projection)
        return ordered[0] if ordered else None

    def _can_dispatch_for_session(self, session_id: str) -> bool:
        session = self._sessions[session_id]
        # Resolve the effective policy live (issue #45): per-session override,
        # else the deployment default, else ImmediatePolicy.
        policy = resolve_effective_policy(session, self._deployment_policy)
        instant = self._clock.now() if self._clock is not None else None
        # When no clock is wired, fall back to immediate-only behavior so
        # legacy test setups (PR #29's _Harness without a clock) keep
        # working. Production always wires SystemClock.
        if instant is None:
            return isinstance(policy, ImmediatePolicy)
        return can_dispatch(
            policy,
            instant,
            manual_drain_remaining=session.manual_drain_remaining,
        )

    def _next_window_opening_iso(self, session_id: str) -> str | None:
        session = self._sessions[session_id]
        if self._clock is None:
            return None
        policy = resolve_effective_policy(session, self._deployment_policy)
        opening = next_window_opening(policy, self._clock.now())
        return opening.isoformat() if opening is not None else None

    async def _run_task(self, task: Task) -> None:
        """Drive the launcher for one task, then emit task.completed/failed.

        Rate-limit failures are retried with exponential backoff (issue #62):
        the in-flight slot stays set during backoff so the dispatcher does not
        start other tasks while waiting for the API to recover.  ``task.retrying``
        is emitted before each sleep so the status is observable.  After the
        5-hour cumulative ceiling, the task is failed with
        ``reason="rate_limit_exhausted"``.

        All other failures are terminal: ``task.failed`` is emitted immediately.
        """
        session = self._sessions[task.session_id]
        effective_model = resolve_effective_model(
            task_model=task.model,
            session_model=session.model,
            deployment_default=(
                self._deployment_model_config.default_model
                if self._deployment_model_config is not None
                else None
            ),
        )

        attempt = 0
        cumulative_wait_s = 0.0
        # conversation_mode for the first run uses the task setting.
        # After the first rate-limit, we always resume from the captured ID.
        current_conversation_mode = task.conversation_mode

        try:
            while True:
                try:
                    await _run_launcher(
                        session=session,
                        session_id=task.session_id,
                        prompt=task.content,
                        get_launcher=self._get_launcher,
                        emitter=self._emitter,
                        propagate_failures=True,
                        model=effective_model,
                        conversation_mode=current_conversation_mode,
                    )
                except RateLimitError as rl_exc:
                    # Capture conversation ID from the failed run so the next
                    # attempt can resume the conversation instead of starting
                    # fresh.  _run_launcher already sets session.last_conversation_id
                    # from stdout; the exc.captured_id is the most recent value,
                    # which may be newer (from the api_retry event).
                    if rl_exc.captured_id is not None:
                        session.last_conversation_id = rl_exc.captured_id

                    # The exponential schedule is a floor on responsiveness;
                    # a usage/session limit that advertises resetsAt overrides
                    # it so we wait until the limit actually resets instead of
                    # hammering it every 30 s.  The cumulative ceiling below
                    # still bounds the total wait at 5 h.
                    delay = backoff_s(attempt)
                    if (
                        rl_exc.retry_after_floor_s is not None
                        and rl_exc.retry_after_floor_s > delay
                    ):
                        delay = rl_exc.retry_after_floor_s
                    cumulative_wait_s += delay

                    if exceeds_ceiling(cumulative_wait_s):
                        await self._emitter.emit(
                            task.session_id,
                            "task.failed",
                            {
                                "task_id": str(task.task_id),
                                "reason": "rate_limit_exhausted",
                            },
                        )
                        return

                    attempt += 1
                    await self._emitter.emit(
                        task.session_id,
                        "task.retrying",
                        {
                            "task_id": str(task.task_id),
                            "attempt": attempt,
                            "retry_after_s": delay,
                            "reason": rl_exc.reason,
                        },
                    )
                    # _in_flight remains set — no other tasks are dispatched
                    # during the backoff sleep.
                    await asyncio.sleep(delay)
                    # Subsequent attempts always resume the conversation.
                    current_conversation_mode = "resume"
                    continue

                # Success — primary run (and auto-sync) completed.
                await self._emitter.emit(
                    task.session_id,
                    "task.completed",
                    {"task_id": str(task.task_id)},
                )
                return

        except Exception as exc:
            await self._emitter.emit(
                task.session_id,
                "task.failed",
                {"task_id": str(task.task_id), "reason": str(exc)},
            )
        finally:
            self._in_flight = None
            self._launch_task = None
            await self._maybe_dispatch_next()
