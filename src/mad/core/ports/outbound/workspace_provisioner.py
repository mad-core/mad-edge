"""Outbound port: WorkspaceProvisioner.

Formal contract for creating and managing isolated workspaces and mounting
resources into them. Enforces CLAUDE.md hard rules 2 (token hygiene) and
3 (path traversal prevention) at the adapter level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkspaceProvisioner(Protocol):
    """Creates, populates, and destroys per-session workspaces."""

    def create(self, session_id: str) -> Path:
        """Return (and create if necessary) the workspace directory for a session."""
        ...

    def destroy(self, session_id: str) -> None:
        """Remove the workspace directory for a session, if it exists."""
        ...

    def materialize_github_repo(
        self,
        workspace: Path,
        mount_path: str,
        repo_url: str,
        token: str | None,
    ) -> None:
        """Clone a GitHub repo into the workspace at mount_path.

        Tokens are stripped from the remote after clone (hard rule 2).
        mount_path values that escape the workspace must be rejected (hard rule 3).
        """
        ...

    def materialize_file(
        self,
        workspace: Path,
        mount_path: str,
        content: str,
    ) -> None:
        """Write a file into the workspace at mount_path.

        mount_path values that escape the workspace must be rejected (hard rule 3).
        """
        ...
