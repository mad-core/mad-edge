"""Rehydrate a Session entity from its persisted JSONL events.

Pure domain helper — no I/O, no port dependencies. Callers read the
events from a SessionRepository and pass them in. Used by GetSession
and ListSessions to recover sessions that are not in the in-memory
index (hard rule 6: JSONL is the source of truth).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from mad.core.orchestration.domain.dispatch_policy import (
    DispatchPolicy,
    InvalidDispatchPolicy,
    policy_from_dict,
)
from mad.core.orchestration.domain.ordering import (
    DEFAULT_PRIORITY,
    InvalidPriority,
    validate_priority,
)
from mad.core.sessions.domain.entities.session import Session


def rehydrate_from_events(session_id: str, events: list[dict[str, Any]]) -> Session:
    """Build a minimal Session entity from its persisted event stream.

    ``created_at`` is the timestamp of the ``session.created`` event (or the
    earliest event if none is present); ``updated_at`` is the timestamp of
    the latest event. Events without a parseable timestamp are skipped for
    the timestamp computation but still drive the status transitions.
    """
    agent: dict[str, Any] = {}
    workspace = ""
    working_directory = ""
    model: str | None = None
    effort: str | None = None
    timeout_s: float | None = None
    # ``None`` means "no per-session override" — the session inherits the
    # operator default (MAD_AUTO_SYNC > True) at resolution time (issue #109).
    auto_sync: bool | None = None
    status = "created"
    created_at: datetime | None = None
    latest_at: datetime | None = None
    # ``None`` means "no per-session override" — the session inherits the
    # deployment default at dispatch time (issue #45). Replaying a
    # ``dispatch_policy.cleared`` event resets it back to ``None``.
    dispatch_policy: DispatchPolicy | None = None
    priority = DEFAULT_PRIORITY

    for event in events:
        etype = event.get("type", "")
        if etype == "session.created":
            agent = {"name": event.get("agent", ""), "provider": event.get("provider") or "unknown"}
            working_directory = event.get("working_directory", "")
            model = event.get("model")
            effort = event.get("effort")
            timeout_s = event.get("timeout_s")
            auto_sync = event.get("auto_sync")
        elif etype == "session.status_running":
            status = "running"
        elif etype == "session.status_idle":
            status = "idle"
        elif etype == "session.error":
            status = "error"
        elif etype == "session.deleted":
            status = "deleted"
        elif etype == "dispatch_policy.updated":
            # ADR-0009 §9 — replay rebuilds Session.dispatch_policy from
            # the persisted event log. Malformed payloads (which shouldn't
            # exist post-validation but defensively): keep current policy.
            try:
                dispatch_policy = policy_from_dict(_event_payload(event))
            except InvalidDispatchPolicy:
                continue
        elif etype == "dispatch_policy.cleared":
            # Issue #45 — DELETE cleared the per-session override; the
            # session goes back to inheriting the deployment default.
            dispatch_policy = None
        elif etype == "dispatch_priority.updated":
            # Issue #46 — replay rebuilds Session.priority from the log,
            # mirroring dispatch_policy.updated above. The JSONL layer
            # flattens event data, so ``priority`` sits at the top level.
            try:
                priority = validate_priority(event.get("priority"))
            except InvalidPriority:
                continue

        ts = _parse_timestamp(event.get("timestamp"))
        if ts is None:
            continue
        if etype == "session.created" and created_at is None:
            created_at = ts
        if latest_at is None or ts > latest_at:
            latest_at = ts

    if created_at is None and latest_at is not None:
        created_at = latest_at
    if created_at is None:
        created_at = datetime.fromtimestamp(0, tz=UTC)
    if latest_at is None:
        latest_at = created_at

    return Session(
        session_id=session_id,
        agent=agent,
        workspace=workspace,
        working_directory=working_directory,
        model=model,
        effort=effort,
        timeout_s=timeout_s,
        auto_sync=auto_sync,
        status=status,
        dispatch_policy=dispatch_policy,
        priority=priority,
        created_at=created_at,
        updated_at=latest_at,
    )


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Extract the policy fields from a persisted event dict.

    The JSONL persistence layer flattens event ``data`` onto the event
    record, so ``kind``/``windows`` live at the event's top level
    (alongside ``type`` and ``timestamp``). We strip the persistence
    metadata before handing the dict to ``policy_from_dict``.
    """
    return {k: v for k, v in event.items() if k not in {"type", "timestamp", "session_id"}}


def _parse_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts
