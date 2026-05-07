"""SessionStore — in-memory index of live sessions.

This class is a thin container providing:
  - sessions: dict[str, Session] — the live session index
  - idempotency: dict[str, str] — key -> session_id map

Use cases receive these dicts directly as constructor arguments so they
can be tested without a SessionStore instance. All persistence is handled
by the SessionRepository port — SessionStore has no I/O.
"""

from __future__ import annotations

from mad.core.sessions.domain.entities.session import Session


class SessionStore:
    """Holds all per-process session state. Injected into create_app() so
    tests (and other embeddings) get a fresh instance with no global leakage.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.idempotency: dict[str, str] = {}
