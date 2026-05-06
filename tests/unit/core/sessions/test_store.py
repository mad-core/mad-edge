"""Unit tests for SessionStore — the per-process in-memory index.

Pure dict container, so a single happy-path test is enough.
"""

from __future__ import annotations

from mad.core.sessions import SessionStore


def test_session_store_starts_empty():
    store = SessionStore()
    assert store.sessions == {}
    assert store.idempotency == {}
