"""Sessions domain exceptions."""

from __future__ import annotations

from mad.core.sessions.domain.exceptions.base import (
    DomainError,
    PathTraversalError,
    SessionNotFound,
)

__all__ = ["DomainError", "PathTraversalError", "SessionNotFound"]
