"""Session entity.

The Session is the primary aggregate root for Mad. It tracks the lifecycle
of a single agent invocation from creation through running, idle, error,
or deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    """Mutable entity representing an agent session.

    Status transitions:
        created -> running -> idle
        created -> running -> error
        any -> deleted
    """

    session_id: str
    agent: dict[str, Any]
    workspace: str
    status: str = "created"
    # Opaque dict of mounted resource descriptors for the HTTP response.
    resources_mounted: list[dict[str, Any]] = field(default_factory=list)
    # Cached HTTP response for idempotency
    response: dict[str, Any] = field(default_factory=dict)
    # Sensitive tokens collected at creation time — used for redaction in agent output.
    # These are NEVER written to the JSONL log (hard rule 2).
    tokens_to_redact: list[str] = field(default_factory=list, repr=False)

    # ---------------------------------------------------------------------------
    # Domain transitions
    # ---------------------------------------------------------------------------

    def mark_running(self) -> None:
        """Transition to running state (agent launched)."""
        self.status = "running"

    def mark_idle(self, stop_reason: str | None = None) -> None:
        """Transition to idle state (agent finished successfully)."""
        self.status = "idle"

    def mark_error(self, reason: str | None = None) -> None:
        """Transition to error state."""
        self.status = "error"

    def mark_deleted(self) -> None:
        """Transition to deleted state."""
        self.status = "deleted"

    # ---------------------------------------------------------------------------
    # Serialization helpers (for SessionStore compatibility)
    # ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the dict format used by SessionStore."""
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "workspace": self.workspace,
            "status": self.status,
            "resources_mounted": self.resources_mounted,
            "response": self.response,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        """Reconstruct from a dict (e.g. from SessionStore or JSONL metadata)."""
        return cls(
            session_id=d["session_id"],
            agent=d.get("agent", {}),
            workspace=d.get("workspace", ""),
            status=d.get("status", "created"),
            resources_mounted=d.get("resources_mounted", []),
            response=d.get("response", {}),
        )
