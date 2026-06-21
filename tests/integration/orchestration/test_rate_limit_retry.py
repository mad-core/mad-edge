"""Integration tests for rate-limit-aware retry with exponential backoff (issue #62).

Covers the Dispatcher + ScriptedLauncher end-to-end:

1. Rate-limit detected → task.retrying emitted → backoff → success.
2. Rate-limit every attempt → task.failed with reason="rate_limit_exhausted"
   after the ceiling is exhausted.
3. Non-rate-limit exception → task.failed immediately, no task.retrying.
4. Conversation ID captured from the failed run → passed to the next attempt
   via session.last_conversation_id.
5. Non-rate-limit errors leave retry_info=None in the projection.

State-based polling per heuristic 7.  No bare time.sleep.
Backoff is forced to ~0 s via monkeypatching so tests finish in < 2 s.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
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


async def _wait_for_event(
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


async def _wait_no_event(
    store: FakeEventStore,
    *,
    session_id: str,
    event_type: str,
    settle_s: float = 0.1,
) -> None:
    """Assert that ``event_type`` does NOT appear within ``settle_s`` seconds."""
    await asyncio.sleep(settle_s)
    found = [c for c in store.calls if c[0] == session_id and c[1] == event_type]
    if found:
        pytest.fail(f"unexpected event {event_type!r} found on {session_id}")


class _Harness:
    def __init__(self, sessions: dict[str, Session], launcher: ScriptedLauncher) -> None:
        self.store = FakeEventStore()
        self.bus = InMemoryEventBus()
        self.projection = InMemoryTaskProjection()
        self.emitter = EventEmitter(store=self.store, bus=self.bus)
        self.sessions = sessions
        self.dispatcher = Dispatcher(
            projection=self.projection,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=sessions,
            get_launcher=lambda _: launcher,
        )
        self.enqueue = EnqueueTaskUseCase(
            sessions_index=sessions,
            emitter=self.emitter,
        )

    async def start(self) -> None:
        await self.dispatcher.start()

    async def stop(self) -> None:
        await self.dispatcher.stop()


# -- Tests ------------------------------------------------------------------


async def test_rate_limit_retry_then_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rate-limit on first attempt → task.retrying emitted → second attempt
    succeeds → task.completed.  Backoff is monkeypatched to ~0 s so the test
    completes in well under the 15 s timeout cap."""
    import mad.core.orchestration.domain.retry_schedule as sched

    monkeypatch.setattr(sched, "_BASE_S", 0.01)
    monkeypatch.setattr(sched, "_JITTER_FRACTION", 0.0)
    monkeypatch.setattr(sched, "_MIN_BACKOFF_S", 0.0)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    captured_conv_id = "conv-retry-success"
    rl_error = RateLimitError(captured_id=captured_conv_id, reason="rate_limit")
    sessions = {"sesn_rl": _session("sesn_rl", workspace)}

    # Run 1: raises RateLimitError (primary + auto-sync both scripted)
    # Run 2 (retry): succeeds for primary
    # Run 3: auto-sync success
    launcher.script_raising(
        [
            rl_error,  # primary fails with rate-limit
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                "conv-retry-success",
            ),  # retry primary succeeds
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                None,
            ),  # auto-sync
        ]
    )

    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_rl", content="do work", conversation_mode="new")
        )
        await _wait_for_event(h.store, session_id="sesn_rl", event_type="task.retrying")
        await _wait_for_event(h.store, session_id="sesn_rl", event_type="task.completed")

        # task.retrying carries attempt=1, reason, retry_after_s
        retrying = next(c for c in h.store.calls if c[0] == "sesn_rl" and c[1] == "task.retrying")
        assert retrying[2]["attempt"] == 1
        assert retrying[2]["reason"] == "rate_limit"
        assert retrying[2]["retry_after_s"] > 0
        assert retrying[2]["retry_after_s"] == pytest.approx(0.01)

        # task.failed must NOT be emitted
        failed = [c for c in h.store.calls if c[0] == "sesn_rl" and c[1] == "task.failed"]
        assert not failed

        # Conversation ID captured from the rate-limited run and used on retry
        assert launcher.calls[1]["conversation_id"] == captured_conv_id
    finally:
        await h.stop()


async def test_rate_limit_ceiling_emits_task_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt fails with rate-limit; after cumulative ceiling, task.failed
    with reason='rate_limit_exhausted'."""
    import mad.core.orchestration.domain.retry_schedule as sched

    monkeypatch.setattr(sched, "_BASE_S", 0.01)
    monkeypatch.setattr(sched, "_JITTER_FRACTION", 0.0)
    monkeypatch.setattr(sched, "_MIN_BACKOFF_S", 0.0)
    # Set ceiling to just above two 0.01 s sleeps so three failures exhaust it.
    monkeypatch.setattr(sched, "CUMULATIVE_CEILING_S", 0.025)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    rl = RateLimitError(captured_id=None, reason="overloaded")
    # Three rate-limit errors → ceiling exceeded after the third attempt's delay.
    launcher.script_raising([rl, rl, rl, rl, rl])

    sessions = {"sesn_ceil": _session("sesn_ceil", workspace)}
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_ceil", content="work", conversation_mode="new")
        )
        await _wait_for_event(h.store, session_id="sesn_ceil", event_type="task.failed")

        failed = next(c for c in h.store.calls if c[0] == "sesn_ceil" and c[1] == "task.failed")
        assert failed[2]["reason"] == "rate_limit_exhausted"

        # task.completed must NOT be emitted
        completed = [c for c in h.store.calls if c[0] == "sesn_ceil" and c[1] == "task.completed"]
        assert not completed
    finally:
        await h.stop()


async def test_non_rate_limit_error_immediate_task_failed(tmp_path: Path) -> None:
    """A non-rate-limit exception produces task.failed immediately without
    any task.retrying event."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    launcher.script_raising(
        [
            RuntimeError("bad prompt"),
        ]
    )

    sessions = {"sesn_nrl": _session("sesn_nrl", workspace)}
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_nrl", content="crash", conversation_mode="new")
        )
        await _wait_for_event(h.store, session_id="sesn_nrl", event_type="task.failed")

        # task.retrying must NOT appear
        retrying = [c for c in h.store.calls if c[0] == "sesn_nrl" and c[1] == "task.retrying"]
        assert not retrying

        failed = next(c for c in h.store.calls if c[0] == "sesn_nrl" and c[1] == "task.failed")
        # Reason is the exception message surfaced verbatim by the dispatcher.
        assert failed[2]["reason"] == "bad prompt"
    finally:
        await h.stop()


async def test_rate_limit_passes_captured_id_to_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conversation ID captured from the rate-limited run is passed to the
    retry attempt via session.last_conversation_id → launcher receives it as
    conversation_id kwarg."""
    import mad.core.orchestration.domain.retry_schedule as sched

    monkeypatch.setattr(sched, "_BASE_S", 0.01)
    monkeypatch.setattr(sched, "_JITTER_FRACTION", 0.0)
    monkeypatch.setattr(sched, "_MIN_BACKOFF_S", 0.0)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    conv_id = "conv-from-failed-run"
    rl = RateLimitError(captured_id=conv_id, reason="rate_limit")
    launcher.script_raising(
        [
            rl,  # first attempt raises — conv_id captured
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                conv_id,
            ),  # retry succeeds
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                None,
            ),  # auto-sync
        ]
    )

    sessions = {"sesn_cid": _session("sesn_cid", workspace)}
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_cid", content="resume work", conversation_mode="new")
        )
        await _wait_for_event(h.store, session_id="sesn_cid", event_type="task.completed")

        # calls[0] = first attempt (conversation_id=None, new)
        assert launcher.calls[0]["conversation_id"] is None
        # calls[1] = retry attempt (conversation_id=conv_id, resumed)
        assert launcher.calls[1]["conversation_id"] == conv_id
    finally:
        await h.stop()


async def test_rate_limit_floor_overrides_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the launcher advertises a retry floor (from resetsAt) larger than
    the backoff interval, the dispatcher waits the floor, not the backoff.
    Negative twin: test_rate_limit_retry_then_success (floor=None) waits the
    plain backoff (~0.01 s)."""
    import mad.core.orchestration.domain.retry_schedule as sched

    # backoff(0) would be ~0.01 s; the floor below (0.05 s) must win.
    monkeypatch.setattr(sched, "_BASE_S", 0.01)
    monkeypatch.setattr(sched, "_JITTER_FRACTION", 0.0)
    monkeypatch.setattr(sched, "_MIN_BACKOFF_S", 0.0)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    rl = RateLimitError(captured_id=None, reason="rate_limit", retry_after_floor_s=0.05)
    launcher.script_raising(
        [
            rl,
            ([{"type": "session.status_idle", "stop_reason": "end_turn"}], None),
            ([{"type": "session.status_idle", "stop_reason": "end_turn"}], None),
        ]
    )

    sessions = {"sesn_floor": _session("sesn_floor", workspace)}
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_floor", content="work", conversation_mode="new")
        )
        await _wait_for_event(h.store, session_id="sesn_floor", event_type="task.retrying")

        retrying = next(
            c for c in h.store.calls if c[0] == "sesn_floor" and c[1] == "task.retrying"
        )
        assert retrying[2]["retry_after_s"] == pytest.approx(0.05), (
            f"floor (0.05 s) must override backoff (0.01 s), got: {retrying[2]['retry_after_s']}"
        )
    finally:
        await h.stop()


async def test_retrying_projection_shows_retry_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """While the dispatcher is sleeping in backoff, the projection exposes
    retry_info with attempt/retry_after_s/reason so the HTTP surface can
    render status='retrying'."""
    import mad.core.orchestration.domain.retry_schedule as sched

    # Use a long enough sleep that we can observe mid-backoff state,
    # but short enough for the test to finish in time.
    monkeypatch.setattr(sched, "_BASE_S", 0.2)
    monkeypatch.setattr(sched, "_JITTER_FRACTION", 0.0)
    monkeypatch.setattr(sched, "_MIN_BACKOFF_S", 0.0)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    rl = RateLimitError(captured_id=None, reason="overloaded")
    launcher.script_raising(
        [
            rl,
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                None,
            ),
            (
                [{"type": "session.status_idle", "stop_reason": "end_turn"}],
                None,
            ),
        ]
    )

    sessions = {"sesn_proj": _session("sesn_proj", workspace)}
    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_proj", content="work", conversation_mode="new")
        )
        # Wait for task.retrying to be emitted to the store.
        await _wait_for_event(h.store, session_id="sesn_proj", event_type="task.retrying")

        # Give the bus loop a tick to apply the event to the projection.
        await asyncio.sleep(0.02)

        ri = h.projection.retry_info("sesn_proj")
        assert ri is not None, "projection should expose retry_info during backoff"
        assert ri.attempt == 1
        assert ri.reason == "overloaded"
        assert ri.retry_after_s > 0
        assert ri.retry_after_s == pytest.approx(0.2)

        # in_flight must still be set (slot not released during backoff).
        assert h.projection.in_flight("sesn_proj") is not None
        assert h.projection.in_flight("sesn_proj").session_id == "sesn_proj"

        # Wait for completion then verify retry_info is cleared.
        await _wait_for_event(h.store, session_id="sesn_proj", event_type="task.completed")
        await asyncio.sleep(0.02)
        assert h.projection.retry_info("sesn_proj") is None
    finally:
        await h.stop()
