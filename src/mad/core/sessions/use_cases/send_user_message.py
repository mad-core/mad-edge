"""SendUserMessage use case.

Handles ``user.message`` events, launching the agent for each message.
Implements token redaction in ``agent.output`` events (CLAUDE.md
hard rule 2).

Every event the use case appends to the repository is also published
to the injected ``EventBus`` so the cross-session observability surface
(issue #10) sees a live copy of every state change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mad.core.events.emitter import EventEmitter
from mad.core.orchestration.domain.exceptions.base import SessionHasInFlightTask
from mad.core.orchestration.ports.task_queue import TaskQueue
from mad.core.sessions.domain.entities.session import Session
from mad.core.sessions.domain.exceptions.base import SessionNotFound
from mad.core.sessions.use_cases.auto_sync_prompt import build_auto_sync_prompt


@dataclass
class SendUserMessageInput:
    session_id: str
    content: str


class SendUserMessageUseCase:
    """Accept a user message and dispatch the agent launcher as a
    background task.

    Token redaction: collects all ``authorization_token`` values from
    the session's ``resources_mounted`` and replaces any occurrence in
    emitted event data with ``[REDACTED]`` before persisting to the
    JSONL log.
    """

    def __init__(
        self,
        sessions_index: dict[str, Session],
        get_launcher: Callable[[str], Any],
        emitter: EventEmitter,
        task_queue: TaskQueue | None = None,
    ) -> None:
        self._sessions = sessions_index
        self._get_launcher = get_launcher
        self._emitter = emitter
        self._task_queue = task_queue

    def execute(self, payload: SendUserMessageInput) -> None:
        """Validate and schedule the agent run. Returns immediately."""
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)
        if self._task_queue is not None:
            in_flight = self._task_queue.in_flight(payload.session_id)
            if in_flight is not None:
                raise SessionHasInFlightTask(payload.session_id, in_flight.task_id)
        session = self._sessions[payload.session_id]
        # Fire-and-forget: emitter handles both persistence and publish.
        asyncio.create_task(
            self._emitter.emit(
                payload.session_id, "user.message", {"content": payload.content}
            )
        )
        asyncio.create_task(
            _run_launcher(
                session=session,
                session_id=payload.session_id,
                prompt=payload.content,
                get_launcher=self._get_launcher,
                emitter=self._emitter,
            )
        )


async def _run_launcher(
    session: Session,
    session_id: str,
    prompt: str,
    get_launcher: Callable[[str], Any],
    emitter: EventEmitter,
    propagate_failures: bool = False,
) -> None:
    """Internal coroutine: run the launcher and handle lifecycle events.

    When ``propagate_failures`` is False (the default, used by
    /messages' fire-and-forget path), launcher exceptions are
    converted to ``session.error`` events and the coroutine returns
    cleanly. When True, after the ``session.error`` is emitted the
    exception is re-raised so a caller (e.g. the orchestration
    Dispatcher) can map the failure to ``task.failed``.
    """
    await emitter.emit(session_id, "session.status_running")
    session.mark_running()

    tokens_to_redact = _collect_tokens(session)

    launcher = get_launcher(session.agent["provider"])
    workspace = Path(session.workspace)

    async def emit(event_type: str, data: dict[str, Any] | None = None) -> None:
        redacted_data = (
            _redact_tokens(data, tokens_to_redact)
            if data and tokens_to_redact
            else data
        )
        await emitter.emit(session_id, event_type, redacted_data)
        if event_type == "session.status_idle":
            session.mark_idle()
        elif event_type == "session.error":
            session.mark_error()

    primary_failure: Exception | None = None
    try:
        await launcher.run(session_id=session_id, prompt=prompt, workspace=workspace, emit=emit)
    except Exception as exc:
        if session.status == "running":
            await emitter.emit(session_id, "session.error", {"error": str(exc)})
            session.mark_error()
        primary_failure = exc

    # Post-run auto-sync (issue #8): always launch a second agent run in
    # the same workspace with a fixed instruction prompt that decides
    # whether to branch / commit / push / open a PR. Mad does not
    # interpret the run's output (hard rule 1); events are emitted as
    # agent.output like any other run. Failures here MUST NOT crash the
    # session task — they are surfaced as a session.error event.
    auto_sync_failure: Exception | None = None
    try:
        auto_sync_prompt = build_auto_sync_prompt(session_id, session.base_branch)
        await launcher.run(session_id=session_id, prompt=auto_sync_prompt, workspace=workspace, emit=emit)
    except Exception as exc:
        await emitter.emit(
            session_id, "session.error", {"error": f"auto-sync failed: {exc}"}
        )
        session.mark_error()
        auto_sync_failure = exc

    if propagate_failures and primary_failure is not None:
        raise primary_failure
    if propagate_failures and auto_sync_failure is not None:
        raise auto_sync_failure


def _collect_tokens(session: Session) -> list[str]:
    """Collect all authorization tokens from session (for redaction).

    Tokens are stored in ``session.tokens_to_redact`` at creation time
    and are NEVER persisted to the JSONL log.
    """
    return [t for t in session.tokens_to_redact if t]


def _redact_tokens(data: dict[str, Any], tokens: list[str]) -> dict[str, Any]:
    """Replace token literals in all string values of ``data`` with ``[REDACTED]``."""
    if not tokens:
        return data
    result = {}
    for k, v in data.items():
        if isinstance(v, str):
            for token in tokens:
                if token:
                    v = v.replace(token, "[REDACTED]")
        result[k] = v
    return result
