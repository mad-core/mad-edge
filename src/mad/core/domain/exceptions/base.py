"""Core domain exceptions.

These are pure domain errors — no framework imports allowed.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for domain exceptions."""


class PathTraversalError(DomainError):
    """Raised when a mount_path would escape the workspace (CLAUDE.md hard rule 3)."""

    def __init__(self, mount_path: str, reason: str) -> None:
        super().__init__(f"invalid mount_path '{mount_path}': {reason}")
        self.mount_path = mount_path
        self.reason = reason


class SessionNotFound(DomainError):
    """Raised when a session cannot be found in the registry or JSONL log."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session '{session_id}' not found")
        self.session_id = session_id
