"""Test-only doubles for the sessions bounded context.

Lives under ``tests/`` per ADR-0003 — production code in ``src/`` does
not carry fakes. Use cases inject these to verify orchestration without
touching the real filesystem or git.

The ``FakeSessionRepository`` here doubles as a (legacy)
``SessionRepository`` AND as an ``EventStore`` so it can drive the
``EventEmitter`` directly. Tests that only need the narrow
``EventStore`` port should use ``support.events.FakeEventStore``.
"""

from __future__ import annotations

from pathlib import Path

from support.events import DualInterfaceEventStore


class FakeSessionRepository(DualInterfaceEventStore):
    """In-memory ``SessionRepository`` + ``EventStore`` double."""


class FakeProvisioner:
    """In-memory ``WorkspaceProvisioner`` double.

    Records every call so tests can assert that resources were
    materialized with the expected mount path, branch, and content.
    """

    def __init__(self, workspace_root: Path | None = None) -> None:
        self._root = workspace_root
        self.created: list[str] = []
        self.destroyed: list[str] = []
        self.files_written: list[tuple[str, str]] = []
        self.repos_cloned: list[tuple[str, str, str | None]] = []

    def create(self, session_id: str) -> Path:
        self.created.append(session_id)
        if self._root is None:
            return Path("/tmp/mad_" + session_id)
        p = self._root / session_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def destroy(self, session_id: str) -> None:
        self.destroyed.append(session_id)

    def materialize_github_repo(
        self,
        workspace: Path,
        mount_path: str,
        repo_url: str,
        token: str | None,
        base_branch: str | None = None,
    ) -> None:
        self.repos_cloned.append((mount_path, repo_url, base_branch))

    def materialize_file(self, workspace: Path, mount_path: str, content: str) -> None:
        self.files_written.append((mount_path, content))
