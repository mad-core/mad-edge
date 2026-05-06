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

    The launcher receives a prompt, a workspace path, and an async emit
    callback. It spawns the external agent, streams stdout line-by-line as
    ``agent.output`` events, and emits ``session.status_idle`` (exit 0) or
    ``session.error`` (non-zero / timeout) on completion.
    """

    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict[str, Any] | None], Coroutine[Any, Any, None]],
    ) -> None: ...
