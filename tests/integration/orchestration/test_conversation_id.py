"""Orchestration integration tests for conversation ID capture and resume (issue #63).

Tests the runtime flow end-to-end through the Dispatcher + ScriptedLauncher:
1. New task → launcher returns a conversation_id → stored on Session.
2. Resume task with stored id → launcher receives the id via ``conversation_id`` kwarg.
3. Resume task with no stored id → ``agent.conversation_resume_skipped`` emitted,
   launcher called with ``conversation_id=None`` (fallback to new).
4. Hook-based capture: launcher emits ``agent.claude_cli.hook.SessionStart`` →
   ``on_emit`` captures ``data.session_id`` even when stdout returns ``None``
   (simulates a run that times out before the ``result`` event).
5. Auto-sync hook non-corruption: the auto-sync run's ``SessionStart`` hook must
   NOT overwrite the primary run's captured conversation ID.

State-based polling per heuristic 7 — no ``time.sleep + assert count``.
Every loop has a deadline + outcome assertion.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from mad.adapters.outbound.events.in_memory_event_bus import InMemoryEventBus
from mad.adapters.outbound.orchestration.projection import InMemoryTaskProjection
from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
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


def _scripted_two_runs(launcher: ScriptedLauncher, conversation_id: str | None = None) -> None:
    """A queued task triggers TWO launcher runs: primary and auto-sync.

    Uses ``script_with_ids`` so the primary run returns ``conversation_id``
    and the auto-sync run returns ``None`` — mirroring real provider behaviour
    where we don't want the auto-sync run to overwrite the primary run's id.
    """
    launcher.script_with_ids(
        [
            ([{"type": "session.status_idle", "stop_reason": "end_turn"}], conversation_id),
            ([{"type": "session.status_idle", "stop_reason": "end_turn"}], None),
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


class _Harness:
    def __init__(
        self,
        sessions: dict[str, Session],
        launcher: ScriptedLauncher,
        on_emit: Callable[[Event], None] | None = None,
    ) -> None:
        self.store = FakeEventStore()
        self.bus = InMemoryEventBus()
        self.projection = InMemoryTaskProjection()
        self.emitter = EventEmitter(store=self.store, bus=self.bus, on_emit=on_emit)
        self.sessions = sessions
        self.launcher_factory: Callable[[str], Any] = lambda _name: launcher
        self.dispatcher = Dispatcher(
            projection=self.projection,
            emitter=self.emitter,
            bus=self.bus,
            sessions_index=sessions,
            get_launcher=self.launcher_factory,
        )
        self.enqueue = EnqueueTaskUseCase(
            sessions_index=sessions,
            emitter=self.emitter,
        )

    async def start(self) -> None:
        await self.dispatcher.start()

    async def stop(self) -> None:
        await self.dispatcher.stop()


def _make_session_start_on_emit(sessions: dict[str, Session]) -> Callable[[Event], None]:
    """Return an ``on_emit`` callback that mirrors ``dependencies.touch_session``'s
    hook-based capture: stores ``data.session_id`` from
    ``agent.claude_cli.hook.SessionStart`` events on the matching session.
    """

    def _on_emit(event: Event) -> None:
        session = sessions.get(event.session_id)
        if (
            session is not None
            and event.type == "agent.claude_cli.hook.SessionStart"
            and event.data is not None
            and isinstance(event.data.get("session_id"), str)
        ):
            session.last_conversation_id = event.data["session_id"]

    return _on_emit


# -- Tests -----------------------------------------------------------------------


async def test_new_task_captures_conversation_id_on_session(tmp_path: Path) -> None:
    """After a successful run the launcher's returned conversation_id is stored
    on ``session.last_conversation_id`` and visible via GET /v1/sessions/{id}."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    conversation_id = "conv-abc-123"
    sessions = {"sesn_a": _session("sesn_a", workspace)}

    _scripted_two_runs(launcher, conversation_id=conversation_id)

    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_a", content="do work", conversation_mode="new")
        )
        await _wait_for_event_type(h.store, session_id="sesn_a", event_type="task.completed")

        assert sessions["sesn_a"].last_conversation_id == conversation_id
    finally:
        await h.stop()


async def test_new_task_emits_agent_conversation_started(tmp_path: Path) -> None:
    """The launcher emits ``agent.conversation_started`` mid-stream; that event
    is persisted to the store for downstream observers."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    conversation_id = "conv-xyz-456"

    # Script primary run to emit conversation_started explicitly
    def _scripted_with_event(launcher: ScriptedLauncher, cid: str) -> None:
        launcher.script_with_ids(
            [
                (
                    [
                        {
                            "type": "agent.conversation_started",
                            "conversation_id": cid,
                            "provider": "fake",
                        },
                        {"type": "session.status_idle", "stop_reason": "end_turn"},
                    ],
                    cid,
                ),
                ([{"type": "session.status_idle", "stop_reason": "end_turn"}], None),
            ]
        )

    sessions = {"sesn_b": _session("sesn_b", workspace)}
    _scripted_with_event(launcher, conversation_id)

    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_b", content="do work", conversation_mode="new")
        )
        await _wait_for_event_type(h.store, session_id="sesn_b", event_type="task.completed")

        conv_events = [
            c for c in h.store.calls if c[0] == "sesn_b" and c[1] == "agent.conversation_started"
        ]
        assert len(conv_events) == 1
        assert conv_events[0][2]["conversation_id"] == conversation_id
    finally:
        await h.stop()


async def test_resume_mode_passes_stored_id_to_launcher(tmp_path: Path) -> None:
    """When ``conversation_mode="resume"`` and the session has a stored id,
    the launcher's ``run()`` receives that id via the ``conversation_id`` kwarg."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    stored_id = "conv-stored-789"

    sessions = {"sesn_r": _session("sesn_r", workspace)}
    sessions["sesn_r"].last_conversation_id = stored_id

    _scripted_two_runs(launcher)  # no new id returned — resume keeps the old one

    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_r", content="continue", conversation_mode="resume")
        )
        await _wait_for_event_type(h.store, session_id="sesn_r", event_type="task.completed")

        # Primary run (index 0) must have received the stored conversation_id.
        assert launcher.calls[0]["conversation_id"] == stored_id
    finally:
        await h.stop()


async def test_resume_with_no_stored_id_falls_back_and_emits_skipped(tmp_path: Path) -> None:
    """When ``conversation_mode="resume"`` but no id is stored yet, the launcher
    falls back to a fresh conversation and emits ``agent.conversation_resume_skipped``."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()

    sessions = {"sesn_s": _session("sesn_s", workspace)}
    # No last_conversation_id set — it's None by default.

    _scripted_two_runs(launcher)

    h = _Harness(sessions, launcher)
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_s", content="try resume", conversation_mode="resume")
        )
        await _wait_for_event_type(
            h.store, session_id="sesn_s", event_type="agent.conversation_resume_skipped"
        )
        await _wait_for_event_type(h.store, session_id="sesn_s", event_type="task.completed")

        # Launcher must have been called with conversation_id=None (fallback to new).
        assert launcher.calls[0]["conversation_id"] is None
        # Skipped event must carry reason.
        skipped = next(
            c
            for c in h.store.calls
            if c[0] == "sesn_s" and c[1] == "agent.conversation_resume_skipped"
        )
        assert skipped[2]["reason"] == "no_conversation_id"
    finally:
        await h.stop()


async def test_hook_session_start_captures_conversation_id(tmp_path: Path) -> None:
    """When the launcher emits ``agent.claude_cli.hook.SessionStart`` mid-run,
    the ``on_emit`` callback (mirroring ``dependencies.touch_session``) captures
    ``data.session_id`` and stores it on ``session.last_conversation_id``.

    The launcher returns ``None`` (simulating a timeout before the ``result``
    event reaches stdout) to confirm that hook-based capture is the primary
    mechanism — independent of stdout parsing.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    hook_session_id = "36849a9e-52bc-462c-8b02-18b879efa310"
    sessions = {"sesn_h": _session("sesn_h", workspace)}

    launcher.script_with_ids(
        [
            (
                [
                    {
                        "type": "agent.claude_cli.hook.SessionStart",
                        "session_id": hook_session_id,
                        "hook_event_name": "SessionStart",
                    },
                    {"type": "session.status_idle", "stop_reason": "end_turn"},
                ],
                None,  # no stdout-captured ID — simulates timeout before result event
            ),
            ([{"type": "session.status_idle", "stop_reason": "end_turn"}], None),
        ]
    )

    h = _Harness(sessions, launcher, on_emit=_make_session_start_on_emit(sessions))
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_h", content="do work", conversation_mode="new")
        )
        await _wait_for_event_type(h.store, session_id="sesn_h", event_type="task.completed")

        assert sessions["sesn_h"].last_conversation_id == hook_session_id
    finally:
        await h.stop()


async def test_autosync_hook_does_not_overwrite_primary_conversation_id(tmp_path: Path) -> None:
    """The auto-sync run's ``SessionStart`` hook MUST NOT overwrite the primary
    run's captured conversation ID.

    ``_run_launcher`` snapshots ``session.last_conversation_id`` after the
    primary run and restores it after the auto-sync run, so that even when
    the auto-sync's hook fires and temporarily stores its own ID, the primary
    ID is preserved for subsequent resume tasks.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    launcher = ScriptedLauncher()
    primary_id = "primary-conv-id"
    autosync_id = "autosync-conv-id"
    sessions = {"sesn_as": _session("sesn_as", workspace)}

    launcher.script_with_ids(
        [
            (
                [
                    {
                        "type": "agent.claude_cli.hook.SessionStart",
                        "session_id": primary_id,
                        "hook_event_name": "SessionStart",
                    },
                    {"type": "session.status_idle", "stop_reason": "end_turn"},
                ],
                None,
            ),
            (
                [
                    {
                        "type": "agent.claude_cli.hook.SessionStart",
                        "session_id": autosync_id,
                        "hook_event_name": "SessionStart",
                    },
                    {"type": "session.status_idle", "stop_reason": "end_turn"},
                ],
                None,
            ),
        ]
    )

    h = _Harness(sessions, launcher, on_emit=_make_session_start_on_emit(sessions))
    await h.start()
    try:
        await h.enqueue.execute(
            EnqueueTaskInput(session_id="sesn_as", content="do work", conversation_mode="new")
        )
        await _wait_for_event_type(h.store, session_id="sesn_as", event_type="task.completed")

        assert sessions["sesn_as"].last_conversation_id == primary_id
    finally:
        await h.stop()
