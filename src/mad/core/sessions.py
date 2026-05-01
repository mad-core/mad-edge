"""SessionStore — in-memory index of live sessions.

This class is a thin container providing:
  - sessions: dict[str, Session] — the live session index
  - idempotency: dict[str, str] — key -> session_id map
  - sse_queues: dict[str, asyncio.Queue] — SSE streams

Use cases receive these dicts directly as constructor arguments so they
can be tested without a SessionStore instance. All persistence is handled
by the SessionRepository port — SessionStore has no I/O.
"""
from __future__ import annotations

import asyncio

from mad.core.domain.entities.session import Session


class SessionStore:
    """Holds all per-process session state. Injected into create_app() so
    tests (and other embeddings) get a fresh instance with no global leakage.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.idempotency: dict[str, str] = {}
        self.sse_queues: dict[str, asyncio.Queue] = {}

    def get_or_create_queue(self, session_id: str) -> asyncio.Queue:
        if session_id not in self.sse_queues:
            self.sse_queues[session_id] = asyncio.Queue()
        return self.sse_queues[session_id]

    def push_event(self, session_id: str, event: dict) -> None:
        q = self.sse_queues.get(session_id)
        if q is not None:
            q.put_nowait(event)
