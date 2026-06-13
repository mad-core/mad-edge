"""Outbound port: AgentLauncher.

Authoritative definition of the interface for launching external agents.
Implementations live in mad.adapters.outbound.agents.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentLauncher(Protocol):
    """Contract that all agent launcher adapters must satisfy.

    The launcher receives Mad's session_id (used to inject context into the
    spawned subprocess, e.g. as MAD_SESSION_ID), a prompt, a workspace path,
    and an async emit callback. It spawns the external agent, streams stdout
    line-by-line as ``agent.output`` events, and emits ``session.status_idle``
    (exit 0) or ``session.error`` (non-zero / timeout) on completion.
    """

    async def run(
        self,
        session_id: str,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict[str, Any] | None], Coroutine[Any, Any, None]],
        model: str | None = None,
    ) -> None:
        """Launch the external agent and stream events via ``emit``.

        ``model`` is an optional model identifier forwarded to the underlying
        CLI (e.g. ``--model`` for claude).  ``None`` means omit the flag and
        let the provider's machine-configured default apply.
        """
        ...
