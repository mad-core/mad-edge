from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol


class AgentLauncher(Protocol):
    async def run(
        self,
        prompt: str,
        workspace: Path,
        emit: Callable[[str, dict | None], Coroutine[Any, Any, None]],
    ) -> None: ...
