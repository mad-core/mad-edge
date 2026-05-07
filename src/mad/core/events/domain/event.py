"""Event domain entity for the cross-session events module.

The events module accepts and emits Mad's existing vocabulary verbatim
(ADR-0004): ``session.created``, ``user.message``, ``session.status_running``,
``agent.output``, ``session.status_idle``, ``session.error``. ``type`` is
left as a free-form string deliberately so new vocabulary can be added
without changing this entity.

``event_id`` may be ``None`` for events written before UUIDv7 minting was
introduced (ADR-0005). The query layer surfaces such events as-is until
they age out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_META_KEYS = frozenset({"event_id", "type", "timestamp"})


@dataclass(frozen=True)
class Event:
    """A single observation in Mad's persisted event log."""

    event_id: UUID | None
    session_id: str
    type: str
    data: dict[str, Any]
    timestamp: datetime


def event_from_persisted(raw: dict[str, Any], session_id: str) -> Event:
    """Build an ``Event`` from the dict shape persisted by
    ``JsonlSessionRepository`` (``{event_id, type, timestamp, **data}``).

    Tolerates legacy lines without ``event_id`` (returns ``event_id=None``)
    and lines without a ``timestamp`` (defaults to the Unix epoch so they
    sort first).
    """
    eid_str = raw.get("event_id")
    eid = UUID(eid_str) if isinstance(eid_str, str) and eid_str else None
    ts_str = raw.get("timestamp")
    if isinstance(ts_str, str) and ts_str:
        ts = datetime.fromisoformat(ts_str)
    else:
        ts = datetime.fromtimestamp(0, tz=UTC)
    type_ = raw.get("type", "")
    data = {k: v for k, v in raw.items() if k not in _META_KEYS}
    return Event(
        event_id=eid,
        session_id=session_id,
        type=type_,
        data=data,
        timestamp=ts,
    )
