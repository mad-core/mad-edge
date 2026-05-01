from __future__ import annotations

from mad.adapters.outbound.agents.claude_cli import ClaudeCLIProvider
from mad.core.ports.outbound.agent_launcher import AgentLauncher


def get_launcher(name: str) -> AgentLauncher:
    if name == "claude_cli":
        return ClaudeCLIProvider()
    raise NotImplementedError(f"Unknown launcher: {name!r}")
