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
from mad.core.orchestration.domain.auto_sync_config import (
    env_auto_sync,
    resolve_effective_auto_sync,
)
from mad.core.orchestration.domain.effort_config import (
    DeploymentEffortConfig,
    resolve_effective_effort,
)
from mad.core.orchestration.domain.exceptions.base import SessionHasInFlightTask
from mad.core.orchestration.domain.exceptions.rate_limit import RateLimitError
from mad.core.orchestration.domain.model_config import (
    DeploymentModelConfig,
    resolve_effective_model,
)
from mad.core.orchestration.domain.timeout_config import (
    env_timeout_s,
    resolve_effective_timeout,
)
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
        deployment_model_config: DeploymentModelConfig | None = None,
        deployment_effort_config: DeploymentEffortConfig | None = None,
    ) -> None:
        self._sessions = sessions_index
        self._get_launcher = get_launcher
        self._emitter = emitter
        self._task_queue = task_queue
        self._deployment_model_config = deployment_model_config
        self._deployment_effort_config = deployment_effort_config

    def execute(self, payload: SendUserMessageInput) -> None:
        """Validate and schedule the agent run. Returns immediately."""
        if payload.session_id not in self._sessions:
            raise SessionNotFound(payload.session_id)
        if self._task_queue is not None:
            in_flight = self._task_queue.in_flight(payload.session_id)
            if in_flight is not None:
                raise SessionHasInFlightTask(payload.session_id, in_flight.task_id)
        session = self._sessions[payload.session_id]
        effective_model = resolve_effective_model(
            task_model=None,
            session_model=session.model,
            deployment_default=(
                self._deployment_model_config.default_model
                if self._deployment_model_config is not None
                else None
            ),
        )
        effective_effort = resolve_effective_effort(
            task_effort=None,
            session_effort=session.effort,
            deployment_default=(
                self._deployment_effort_config.default_effort
                if self._deployment_effort_config is not None
                else None
            ),
        )
        effective_timeout = resolve_effective_timeout(
            session_timeout_s=session.timeout_s,
            env_timeout_s=env_timeout_s(),
        )
        # /messages is the ad-hoc path — it has no task, so there is no task-level
        # override to consult (issue #109).
        effective_auto_sync = resolve_effective_auto_sync(
            task_auto_sync=None,
            session_auto_sync=session.auto_sync,
            env_auto_sync=env_auto_sync(),
        )
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
                model=effective_model,
                effort=effective_effort,
                timeout_s=effective_timeout,
                auto_sync=effective_auto_sync,
            )
        )


async def _run_launcher(
    session: Session,
    session_id: str,
    prompt: str,
    get_launcher: Callable[[str], Any],
    emitter: EventEmitter,
    propagate_failures: bool = False,
    model: str | None = None,
    effort: str | None = None,
    timeout_s: float | None = None,
    conversation_mode: str = "new",
    auto_sync: bool = True,
) -> None:
    """Internal coroutine: run the launcher and handle lifecycle events.

    When ``propagate_failures`` is False (the default, used by
    /messages' fire-and-forget path), launcher exceptions are
    converted to ``session.error`` events and the coroutine returns
    cleanly. When True, after the ``session.error`` is emitted the
    exception is re-raised so a caller (e.g. the orchestration
    Dispatcher) can map the failure to ``task.failed``.

    ``model`` is the resolved effective model to forward to the launcher.
    ``None`` means omit ``--model`` and use the provider's own default.

    ``effort`` is the resolved effective reasoning effort (issue #60),
    forwarded to the launcher as ``--effort`` (claude) / ``--variant``
    (opencode). ``None`` means omit the flag and use the provider's default.

    ``timeout_s`` is the resolved wall-clock budget (issue #61): per-session
    override > ``MAD_AGENT_TIMEOUT_S`` env > 600 s. Forwarded to BOTH the
    primary run and the post-run auto-sync run so a session's timeout applies
    uniformly. ``None`` lets the provider apply its own 600 s fallback.

    The post-run auto-sync (issue #8) is best-effort once the primary has
    run: a ``RateLimitError`` raised by the auto-sync invocation is NOT
    propagated (it would otherwise make the dispatcher re-run the whole
    coroutine and re-execute the already-successful primary prompt —
    issue #87). It is surfaced as a non-terminal ``agent.autosync.rate_limited``
    event and swallowed. Any other auto-sync failure still emits
    ``session.error`` and, under ``propagate_failures``, is re-raised.

    ``conversation_mode`` controls whether to start a fresh conversation
    (``"new"``, default) or continue a previous one (``"resume"``).
    When resuming, ``session.last_conversation_id`` is passed to the
    launcher.  If no ID is stored yet, falls back to ``"new"`` and emits
    ``agent.conversation_resume_skipped``.

    ``auto_sync`` is the resolved post-run publish gate (issue #109): task >
    session > ``MAD_AUTO_SYNC`` env > ``True``. When ``False``, the post-run
    auto-sync run is skipped ENTIRELY — no second ``launcher.run``, so no
    ``mad/<session_id>`` branch and no PR can be created. This is the
    deterministic opt-out for tasks that manage their own named branch/PR, which
    auto-sync cannot see and would otherwise duplicate. A non-terminal
    ``agent.autosync.skipped`` event records the decision for operators.
    """
    await emitter.emit(session_id, "session.status_running")
    session.mark_running()

    tokens_to_redact = _collect_tokens(session)

    launcher = get_launcher(session.agent["provider"])
    workspace = Path(session.working_directory or session.workspace)

    # Resolve resume ID before the run starts.
    resume_id: str | None = None
    if conversation_mode == "resume":
        if session.last_conversation_id is not None:
            resume_id = session.last_conversation_id
        else:
            await emitter.emit(
                session_id,
                "agent.conversation_resume_skipped",
                {"reason": "no_conversation_id"},
            )
            # Fall back to a fresh conversation.

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
        captured_id = await launcher.run(
            session_id=session_id,
            prompt=prompt,
            workspace=workspace,
            emit=emit,
            model=model,
            effort=effort,
            conversation_id=resume_id,
            timeout_s=timeout_s,
        )
        if captured_id is not None:
            session.last_conversation_id = captured_id
    except RateLimitError:
        if propagate_failures:
            # Let the dispatcher catch this and drive the retry loop.
            # Do NOT emit session.error — this run is not terminal yet.
            raise
        # Fire-and-forget path (/messages): no dispatcher to retry, so
        # treat rate-limit as a terminal session error.
        if session.status == "running":
            await emitter.emit(
                session_id,
                "session.error",
                {"error": "rate limit reached; retry not available on /messages path"},
            )
            session.mark_error()
        # primary_failure stays None so auto-sync still runs.
    except Exception as exc:
        if session.status == "running":
            await emitter.emit(session_id, "session.error", {"error": str(exc)})
            session.mark_error()
        primary_failure = exc

    # Post-run auto-sync (issue #8): launch a second agent run in the same
    # workspace with a fixed instruction prompt that decides whether to
    # branch / commit / push / open a PR. Mad does not interpret the run's
    # output (hard rule 1); events are emitted as agent.output like any
    # other run. Failures here MUST NOT crash the session task — they are
    # surfaced as a session.error event.
    #
    # The gate (issue #109): when ``auto_sync`` resolves to False the second
    # run never starts, so no ``mad/<session_id>`` branch and no PR can be
    # created. A task that manages its own named branch/PR sets this; auto-sync
    # cannot see that branch and would otherwise open a duplicate PR next to it.
    auto_sync_failure: Exception | None = None
    if not auto_sync:
        # Record the skip rather than leaving an unexplained absence in the log.
        # Non-terminal: the session's status is whatever the primary run left.
        await emitter.emit(session_id, "agent.autosync.skipped", {"reason": "disabled"})
    else:
        # Snapshot the primary run's conversation ID before the auto-sync run.
        # The auto-sync run starts its own Claude subprocess which fires a
        # SessionStart hook that, via on_emit, would overwrite
        # session.last_conversation_id with the auto-sync's ID. Snapshotting
        # here and restoring below preserves the primary conversation ID.
        primary_conversation_id = session.last_conversation_id
        try:
            auto_sync_prompt = build_auto_sync_prompt(session_id, session.base_branch)
            await launcher.run(
                session_id=session_id,
                prompt=auto_sync_prompt,
                workspace=workspace,
                emit=emit,
                model=model,
                effort=effort,
                timeout_s=timeout_s,
            )
        except RateLimitError as exc:
            # The primary run already succeeded; the post-run auto-sync is a
            # best-effort publish step. A rate limit HERE must NOT propagate as
            # a RateLimitError, because the dispatcher's retry loop would catch
            # it and re-run _run_launcher from the top — re-executing the
            # already-successful primary prompt (issue #87). Surface a distinct,
            # non-terminal signal and swallow it: do NOT emit session.error, do
            # NOT mark_error, and (crucially) do NOT store it in
            # auto_sync_failure so it is never re-raised. The dispatcher then
            # records task.completed for the primary work that did succeed.
            await emitter.emit(
                session_id,
                "agent.autosync.rate_limited",
                {"reason": exc.reason},
            )
        except Exception as exc:
            await emitter.emit(
                session_id, "session.error", {"error": f"auto-sync failed: {exc}"}
            )
            session.mark_error()
            auto_sync_failure = exc

        # Restore the primary run's conversation ID — the auto-sync run's
        # SessionStart hook may have overwritten it via on_emit.
        session.last_conversation_id = primary_conversation_id

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
