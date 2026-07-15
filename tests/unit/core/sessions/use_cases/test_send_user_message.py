"""Unit tests for SendUserMessageUseCase.

Tests the synchronous validation path. The async launcher run is tested
via integration tests.
"""

from __future__ import annotations

import asyncio

import pytest

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.auto_sync_config import AUTO_SYNC_ENV_VAR
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.send_user_message import (
    SendUserMessageInput,
    SendUserMessageUseCase,
    _redact_tokens,
)
from support.events import FakeEventBus
from support.launchers import RecordingLauncher
from support.sessions import FakeSessionRepository as FakeRepo


def _make_session(session_id="sesn_msg", tokens=None, auto_sync=None):
    return Session(
        session_id=session_id,
        agent={"name": "t", "provider": "fake"},
        workspace="/tmp/mad_sesn_msg",
        tokens_to_redact=tokens or [],
        auto_sync=auto_sync,
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
    # Opt in to auto-sync (off by default, issue #109) so the launcher runs
    # twice — this test asserts on the two-run event sequence below.
    sessions = {"sesn_msg": _make_session(tokens=[token], auto_sync=True)}
    bus = FakeEventBus()

    class ScriptedLauncher:
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
            await emit("agent.output", {"line": f"leak {token} bye"})
            await emit("session.status_idle", {"stop_reason": "end_turn"})
            return None

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
    # Auto-sync is off by default (issue #109); opt in so the second run fires.
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    sessions["sesn_msg"].base_branch = "develop"
    bus = FakeEventBus()

    launcher = RecordingLauncher()

    uc = _make_uc(sessions, lambda name: launcher, repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hello"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while len(launcher.calls) < 2 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert len(launcher.calls) == 2
    assert launcher.calls[0] == "hello"
    assert "auto-sync" in launcher.calls[1].lower()
    assert "develop" in launcher.calls[1]
    assert ".claude/settings.local.json" in launcher.calls[1]
    assert ".claude/settings.json" in launcher.calls[1]


async def test_post_run_auto_sync_runs_even_when_primary_fails():
    """Auto-sync must run even when the primary launcher raises (issue #8)."""
    repo = FakeRepo()
    # Opt in to auto-sync (off by default, issue #109): this test asserts the
    # second run fires even after the primary fails.
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    bus = FakeEventBus()

    calls: list[str] = []

    class FlakyLauncher:
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
            calls.append(prompt)
            if len(calls) == 1:
                raise RuntimeError("primary boom")
            await emit("session.status_idle", {"stop_reason": "end_turn"})
            return None

    uc = _make_uc(sessions, lambda name: FlakyLauncher(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hi"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while len(calls) < 2 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)

    assert len(calls) == 2, "second (auto-sync) run must fire even after primary failure"


async def test_post_run_auto_sync_failure_emits_session_error():
    """If the auto-sync run itself raises, surface it as session.error (issue #8)."""
    repo = FakeRepo()
    # Opt in to auto-sync (off by default, issue #109) so the failing second run
    # under test actually runs.
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    bus = FakeEventBus()

    calls: list[int] = []

    class AutoSyncBoom:
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
            calls.append(1)
            if len(calls) == 1:
                await emit("session.status_idle", {"stop_reason": "end_turn"})
                return None
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


async def test_post_run_auto_sync_rate_limit_is_non_terminal_not_session_error():
    """A RateLimitError from the auto-sync run is NOT a session.error.

    Negative twin of ``test_post_run_auto_sync_failure_emits_session_error``:
    a *non*-rate-limit auto-sync failure is terminal (session.error +
    status=error); a *rate-limit* auto-sync failure (issue #87) is best-effort
    and surfaces the non-terminal ``agent.autosync.rate_limited`` instead,
    leaving the session idle (the primary run succeeded).
    """
    repo = FakeRepo()
    # Opt in to auto-sync (off by default, issue #109) so the rate-limited second
    # run under test actually runs.
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    bus = FakeEventBus()

    calls: list[int] = []

    class AutoSyncRateLimited:
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
            calls.append(1)
            if len(calls) == 1:
                await emit("session.status_idle", {"stop_reason": "end_turn"})
                return None
            raise RateLimitError(captured_id=None, reason="overloaded")

    uc = _make_uc(sessions, lambda name: AutoSyncRateLimited(), repo, bus)

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="hi"))
    deadline = asyncio.get_event_loop().time() + 2.0
    while len(calls) < 2 and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.05)
    # Let the auto-sync branch finish emitting after the second run raises.
    await asyncio.sleep(0.1)

    types = [e["type"] for e in repo.events]
    assert "agent.autosync.rate_limited" in types
    rate_limited = next(e for e in repo.events if e["type"] == "agent.autosync.rate_limited")
    assert rate_limited["reason"] == "overloaded"
    # A rate-limited auto-sync must NOT masquerade as a terminal session error.
    assert "session.error" not in types
    assert sessions["sesn_msg"].status == "idle"


# ---------------------------------------------------------------------------
# Post-run auto-sync gate (issue #109)
#
# When the resolved gate is False the second (auto-sync) launcher.run is skipped
# ENTIRELY — one invocation, not two — so no `mad/<session_id>` branch and no
# duplicate PR can be created. The skip is recorded as a non-terminal
# `agent.autosync.skipped` event rather than left as an unexplained absence.
#
# Every assertion here is on the launcher CALL COUNT and the event log after
# polling on a state predicate — never on elapsed time (rule 7).
# ---------------------------------------------------------------------------

_GATE_DEADLINE_S = 2.0


async def _wait_for_event(repo: FakeRepo, event_type: str) -> None:
    """Block until ``event_type`` lands in the repo, or fail with the types seen.

    Bounded by ``_GATE_DEADLINE_S`` (rule 8) and asserts the outcome explicitly
    rather than falling through silently (rule 7).
    """
    deadline = asyncio.get_event_loop().time() + _GATE_DEADLINE_S
    while asyncio.get_event_loop().time() < deadline:
        if any(e["type"] == event_type for e in repo.events):
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        f"timed out waiting for {event_type!r}; got {[e['type'] for e in repo.events]}"
    )


async def _wait_for_calls(launcher: RecordingLauncher, count: int) -> None:
    deadline = asyncio.get_event_loop().time() + _GATE_DEADLINE_S
    while asyncio.get_event_loop().time() < deadline:
        if len(launcher.calls) >= count:
            return
        await asyncio.sleep(0.01)
    pytest.fail(f"timed out waiting for {count} launcher runs; got {launcher.calls}")


async def test_session_auto_sync_false_skips_the_post_run_launcher_run():
    """A session with ``auto_sync=False`` invokes the launcher EXACTLY ONCE.

    ``agent.autosync.skipped`` is the last event ``_run_launcher`` emits on this
    path, so once it is in the log the coroutine has run to completion — the call
    count below is final, not a snapshot mid-flight.
    """
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=False)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_event(repo, "agent.autosync.skipped")

    assert len(launcher.calls) == 1, (
        "auto_sync=False must skip the post-run auto-sync run entirely; "
        f"launcher was invoked with {launcher.calls}"
    )
    assert launcher.calls[0] == "do work"
    skipped = next(e for e in repo.events if e["type"] == "agent.autosync.skipped")
    assert skipped["reason"] == "disabled"


async def test_session_auto_sync_true_still_runs_the_post_run_launcher_run():
    """Negative twin: with the gate ON the behaviour is unchanged — the launcher
    runs twice and NO skip event is recorded."""
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_calls(launcher, 2)

    assert len(launcher.calls) == 2
    assert launcher.calls[0] == "do work"
    assert "auto-sync" in launcher.calls[1].lower()
    types = [e["type"] for e in repo.events]
    assert "agent.autosync.skipped" not in types


async def test_auto_sync_defaults_off_when_session_leaves_it_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """``auto_sync=None`` (the default) inherits the OFF default (issue #109) —
    auto-sync is opt-IN, so an ad-hoc session does not open an unrequested PR. The
    launcher runs exactly once and the skip is recorded."""
    monkeypatch.delenv(AUTO_SYNC_ENV_VAR, raising=False)
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=None)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_event(repo, "agent.autosync.skipped")

    assert len(launcher.calls) == 1
    skipped = next(e for e in repo.events if e["type"] == "agent.autosync.skipped")
    assert skipped["reason"] == "disabled"


async def test_env_auto_sync_false_skips_post_run_run_when_session_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """The operator env default applies on the /messages path when the session
    has no override — resolution there is session > env > True (no task level)."""
    monkeypatch.setenv(AUTO_SYNC_ENV_VAR, "false")
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=None)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_event(repo, "agent.autosync.skipped")

    assert len(launcher.calls) == 1


async def test_session_auto_sync_true_beats_env_false(monkeypatch: pytest.MonkeyPatch):
    """Negative twin of the env case: a session that explicitly opts IN overrides
    an operator env default of ``false`` — the session level is more specific."""
    monkeypatch.setenv(AUTO_SYNC_ENV_VAR, "false")
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=True)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_calls(launcher, 2)

    assert len(launcher.calls) == 2
    assert "agent.autosync.skipped" not in [e["type"] for e in repo.events]


async def test_skipped_auto_sync_is_non_terminal_and_leaves_the_session_idle():
    """The skip must not masquerade as a failure: the session's status is whatever
    the primary run left it (idle here), and no ``session.error`` is emitted."""
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session(auto_sync=False)}
    launcher = RecordingLauncher()
    uc = _make_uc(sessions, lambda name: launcher, repo, FakeEventBus())

    uc.execute(SendUserMessageInput(session_id="sesn_msg", content="do work"))
    await _wait_for_event(repo, "agent.autosync.skipped")

    types = [e["type"] for e in repo.events]
    assert "session.error" not in types
    assert sessions["sesn_msg"].status == "idle"


async def test_send_message_records_session_error_when_launcher_raises():
    repo = FakeRepo()
    sessions = {"sesn_msg": _make_session()}
    bus = FakeEventBus()

    class BoomLauncher:
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
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
        async def run(
            self, session_id, prompt, workspace, emit, model=None, effort=None, conversation_id=None, timeout_s=None
        ):
            await emit("agent.output", {"line": "hi"})
            await emit("session.status_idle", {"stop_reason": "end_turn"})
            return None

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
    # Every event appended to the repo must also appear on the bus, in the same
    # order. Auto-sync is off by default (issue #109), so the single launcher run
    # is followed by a non-terminal `agent.autosync.skipped` — both the run's
    # events and the skip are persisted and published.
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
