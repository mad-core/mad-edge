"""Unit tests for SendUserMessageUseCase.

Tests the synchronous validation path. The async launcher run is tested
via integration tests.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any
from uuid import UUID

import pytest

from mad.core.events.domain.event import Event
from mad.core.events.emitter import EventEmitter
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.send_user_message import (
    SendUserMessageInput,
    SendUserMessageUseCase,
    _redact_tokens,
)
from support.events import FakeEventBus

_EPOCH = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
_NULL_UUID = UUID("00000000-0000-0000-0000-000000000000")


class FakeRepo:
    """In-memory EventStore double.

    Satisfies both the old ``append_event`` interface (used in assertions)
    and the new ``EventStore.append`` interface consumed by ``EventEmitter``.
    """

    def __init__(self):
        self.events: list[dict] = []

    def append_event(self, session_id: str, event_type: str, data: dict | None = None) -> dict:
        event = {"type": event_type, **(data or {})}
        self.events.append(event)
        return event

    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any] | None = None,
    ) -> Event:
        """EventStore.append — persist and return a typed Event."""
        raw = self.append_event(session_id, type, data)
        return Event(
            event_id=_NULL_UUID,
            session_id=session_id,
            type=type,
            data=data or {},
            timestamp=_EPOCH,
        )

    def read_events(self, session_id: str) -> list[dict]:
        return self.events

    def exists(self, session_id: str) -> bool:
        return True


def _make_session(session_id="sesn_msg", tokens=None):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_sesn_msg",
        tokens_to_redact=tokens or [],
    )


def _make_uc(sessions, get_launcher, repo, bus):
    emitter = EventEmitter(store=repo, bus=bus)
    return SendUserMessageUseCase(
        sessions_index=sessions,
        get_launcher=get_launcher,
        emitter=emitter,
    )


def test_send_message_session_not_found():
    sessions: dict = {}
    uc = _make_uc(sessions, lambda name: None, FakeRepo(), FakeEventBus())
    with pytest.raises(SessionNotFound):
        uc.execute(SendUserMessageInput(session_id="sesn_missing", content="hi"))


async def test_send_message_runs_launcher_and_redacts_tokens():
    """Background task drives the full lifecycle: status_running →
    forward emitted events through redaction → status_idle on success.
    """
    repo = FakeRepo()
    token = "ghp_secretXYZ"
    sessions = {"sesn_msg": _make_session(tokens=[token])}
    bus = FakeEventBus()

    class ScriptedLauncher:
        async def run(self, prompt, workspace, emit):
            await emit("agent.output", {"line": f"leak {token} bye"})
            await emit("session.status_idle", {"stop_reason": "end_turn"})

    uc = _make_uc(sessions, lambda name: ScriptedLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hello"))
    # Wait until the background task finishes (two launcher runs = two status_idle).
    # Poll until status reaches a terminal state (not "created" or "running").
    deadline = asyncio.get_event_loop().time() + 2.0
    while (
        sessions["sesn_msg"].status in ("created", "running")
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)
    # Give the second auto-sync run a moment to also complete.
    await asyncio.sleep(0.1)

    types = [e["type"] for e in repo.events]
    # Primary run + post-run auto-sync run (issue #8): the launcher is invoked
    # twice, so two status_idle events are emitted.
    assert types == [
        "user.message",
        "session.status_running",
        "agent.output",
        "session.status_idle",
        "agent.output",
        "session.status_idle",
    ]
    assert sessions["sesn_msg"].status == "idle"
    output_events = [e for e in repo.events if e["type"] == "agent.output"]
    for output_event in output_events:
        assert token not in output_event["line"]
        assert "[REDACTED]" in output_event["line"]


async def test_post_run_auto_sync_invokes_second_launcher_run():
    """After the primary run, send_user_message must invoke the launcher a
    second time with the auto-sync instruction prompt (issue #8).
    """
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    sessions["sesn_msg"].base_branch = "develop"
    bus = FakeEventBus()

    calls: list[str] = []

    class RecordingLauncher:
        async def run(self, prompt, workspace, emit):
            calls.append(prompt)
            await emit("session.status_idle", {"stop_reason": "end_turn"})

    uc = _make_uc(sessions, lambda name: RecordingLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hello"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while len(calls) < 2 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert len(calls) == 2
    assert calls[0] == "hello"
    assert "auto-sync" in calls[1].lower()
    assert "develop" in calls[1]
    assert ".claude/settings.local.json" in calls[1]
    assert ".claude/settings.json" in calls[1]


async def test_post_run_auto_sync_runs_even_when_primary_fails():
    """Auto-sync must run even when the primary launcher raises (issue #8)."""
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    bus = FakeEventBus()

    calls: list[str] = []

    class FlakyLauncher:
        async def run(self, prompt, workspace, emit):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("primary boom")
            await emit("session.status_idle", {"stop_reason": "end_turn"})

    uc = _make_uc(sessions, lambda name: FlakyLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hi"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while len(calls) < 2 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert len(calls) == 2, "second (auto-sync) run must fire even after primary failure"


async def test_post_run_auto_sync_failure_emits_session_error():
    """If the auto-sync run itself raises, surface it as session.error (issue #8)."""
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    bus = FakeEventBus()

    calls: list[int] = []

    class AutoSyncBoom:
        async def run(self, prompt, workspace, emit):
            calls.append(1)
            if len(calls) == 1:
                await emit("session.status_idle", {"stop_reason": "end_turn"})
                return
            raise RuntimeError("sync boom")

    uc = _make_uc(sessions, lambda name: AutoSyncBoom(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hi"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while (
        sessions["sesn_msg"].status not in ("idle", "error")
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)

    error_events = [e for e in repo.events if e["type"] == "session.error"]
    assert any("auto-sync failed" in e.get("error", "") for e in error_events)
    assert sessions["sesn_msg"].status == "error"


async def test_send_message_records_session_error_when_launcher_raises():
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    bus = FakeEventBus()

    class BoomLauncher:
        async def run(self, prompt, workspace, emit):
            raise RuntimeError("kaboom")

    uc = _make_uc(sessions, lambda name: BoomLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hi"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while (
        sessions["sesn_msg"].status not in ("idle", "error")
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)

    types = [e["type"] for e in repo.events]
    assert "session.error" in types
    assert sessions["sesn_msg"].status == "error"


async def test_publishes_every_appended_event_to_the_event_bus():
    """Issue #10 acceptance: SendUserMessage publishes to the injected
    EventBus for every event it appends to the repository — the
    ``user.message`` it appends synchronously and every lifecycle event
    emitted during the launcher run."""
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    bus = FakeEventBus()

    class ScriptedLauncher:
        async def run(self, prompt, workspace, emit):
            await emit("agent.output", {"line": "hi"})
            await emit("session.status_idle", {"stop_reason": "end_turn"})

    uc = _make_uc(sessions, lambda name: ScriptedLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="please"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while (
        sessions["sesn_msg"].status in ("created", "running")
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.1)

    repo_types = [e["type"] for e in repo.events]
    bus_types = [e.type for e in bus.published]
    # Every event appended to the repo must also appear on the bus, in
    # the same order. Auto-sync (issue #8) fires a second launcher run,
    # so the launcher emits `agent.output` and `session.status_idle`
    # twice in a row — both are persisted and published.
    assert repo_types == bus_types
    assert all(e.session_id == "sesn_msg" for e in bus.published)


@pytest.mark.parametrize(
    "data, tokens, check",
    [
        (
            {"line": "output containing ghp_secret and more"},
            ["ghp_secret"],
            lambda r: "ghp_secret" not in r["line"] and "[REDACTED]" in r["line"],
        ),
        (
            {"count": 42, "flag": True},
            ["ghp_secret"],
            lambda r: r["count"] == 42 and r["flag"] is True,
        ),
        (
            {"line": "nothing to redact"},
            [],
            lambda r: r == {"line": "nothing to redact"},
        ),
    ],
    ids=["string-redacted", "non-string-unchanged", "empty-tokens-unchanged"],
)
def test_redact_tokens(data, tokens, check):
    result = _redact_tokens(data, tokens)
    assert check(result)
