"""Session entity.

The Session is the primary aggregate root for Mad. It tracks the lifecycle
of a single agent invocation from creation through running, idle, error,
or deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mad.core.orchestration.domain.dispatch_policy import (
    DispatchPolicy,
)
from mad.core.orchestration.domain.ordering import DEFAULT_PRIORITY


_SENTINEL = datetime.fromtimestamp(0, tz=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    working_directory: str = ""
    status: str = "created"
    base_branch: str | None = None
    model: str | None = None
    resources_mounted: list[dict[str, Any]] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)
    tokens_to_redact: list[str] = field(default_factory=list, repr=False)
    dispatch_policy: DispatchPolicy | None = field(default=None, repr=False)
    manual_drain_remaining: int = field(default=0, repr=False)
    # Cross-session dispatch priority (issue #46): higher dispatches
    # first; [1, 10]; 1 (lowest) when never set, so an explicitly
    # prioritized session always outranks an unprioritized one.
    priority: int = DEFAULT_PRIORITY
    # Last conversation ID returned by the launcher after a successful run.
    # None until a run captures one. Not persisted to JSONL in v1 — the event
    # log carries agent.conversation_started which is the authoritative record;
    # in-memory recovery across restarts is deferred (issue #63).
    last_conversation_id: str | None = None
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = _SENTINEL

    def __post_init__(self) -> None:
        # Align updated_at with created_at when the caller leaves it at
        # default — separate ``default_factory`` calls would otherwise
        # produce two ``now()`` values that differ in microseconds.
        if self.updated_at is _SENTINEL:
            self.updated_at = self.created_at
        if not self.working_directory:
            self.working_directory = self.workspace

    # ---------------------------------------------------------------------------
    # Domain transitions
    # ---------------------------------------------------------------------------

    def mark_running(self) -> None:
        self.status = "running"

    def mark_idle(self, stop_reason: str | None = None) -> None:
        self.status = "idle"

    def mark_error(self, reason: str | None = None) -> None:
        self.status = "error"

    def mark_deleted(self) -> None:
        self.status = "deleted"

    def touch(self, timestamp: datetime) -> None:
        """Bump ``updated_at`` to ``timestamp`` if it is more recent.

        Out-of-order events do not pull the timestamp backwards; the JSONL
        log can replay older events into a live entity during rehydration.
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp > self.updated_at:
            self.updated_at = timestamp

    # ---------------------------------------------------------------------------
    # Serialization helpers (for SessionStore compatibility)
    # ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "workspace": self.workspace,
            "working_directory": self.working_directory,
            "status": self.status,
            "base_branch": self.base_branch,
            "model": self.model,
            "resources_mounted": self.resources_mounted,
            "response": self.response,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Session:
        created_raw = d.get("created_at")
        updated_raw = d.get("updated_at")
        created_at = _parse_iso(created_raw) if created_raw else _utc_now()
        updated_at = _parse_iso(updated_raw) if updated_raw else created_at
        return cls(
            session_id=d["session_id"],
            agent=d.get("agent", {}),
            workspace=d.get("workspace", ""),
            working_directory=d.get("working_directory", ""),
            status=d.get("status", "created"),
            base_branch=d.get("base_branch"),
            model=d.get("model"),
            resources_mounted=d.get("resources_mounted", []),
            response=d.get("response", {}),
            priority=d.get("priority", DEFAULT_PRIORITY),
            created_at=created_at,
            updated_at=updated_at,
        )


def _parse_iso(value: str) -> datetime:
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
