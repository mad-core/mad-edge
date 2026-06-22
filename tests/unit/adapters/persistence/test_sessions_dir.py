"""Unit tests for the session-log directory resolver (#13).

``sessions_dir()`` anchors every session JSONL log. Issue #13 made it an
operator-tunable knob: the ``MAD_SESSIONS_DIR`` env var wins when set, and an
unset (or blank) value falls back to the historical ``Path("sessions")`` so
local dev is unchanged. Resolution is dynamic (read on every call) so an
override applied after import — by an operator or a test — is honored at
runtime rather than frozen at module-import time. Each branch is pinned here
with its negative twin so a regression in the precedence cannot pass unnoticed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mad.adapters.outbound.persistence.jsonl_session_repository import (
    DEFAULT_SESSIONS_DIR,
    SESSIONS_DIR_ENV,
    log_path,
    sessions_dir,
)


def test_uses_mad_sessions_dir_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSIONS_DIR_ENV, "/data/mad-sessions")

    assert sessions_dir() == Path("/data/mad-sessions")


def test_falls_back_to_sessions_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Negative twin of the override path: with the variable unset, resolution
    # must return the historical ``Path("sessions")`` so local dev is unchanged.
    monkeypatch.delenv(SESSIONS_DIR_ENV, raising=False)

    assert sessions_dir() == DEFAULT_SESSIONS_DIR
    assert sessions_dir() == Path("sessions")


def test_blank_mad_sessions_dir_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative twin: a whitespace-only value must NOT win — it falls through to
    # the ``sessions`` default exactly as an unset variable would.
    monkeypatch.setenv(SESSIONS_DIR_ENV, "   ")

    assert sessions_dir() == DEFAULT_SESSIONS_DIR


def test_override_is_resolved_dynamically_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The resolver must read the env on every call, not cache at import time:
    # a value set AFTER the module is imported must take effect immediately.
    monkeypatch.setenv(SESSIONS_DIR_ENV, "/first")
    assert sessions_dir() == Path("/first")

    monkeypatch.setenv(SESSIONS_DIR_ENV, "/second")
    assert sessions_dir() == Path("/second")


def test_log_path_honors_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SESSIONS_DIR_ENV, "/data/mad-sessions")

    assert log_path("sesn_abc") == Path("/data/mad-sessions/sesn_abc.jsonl")


def test_log_path_falls_back_to_sessions_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative twin of ``test_log_path_honors_override``: unset -> default root.
    monkeypatch.delenv(SESSIONS_DIR_ENV, raising=False)

    assert log_path("sesn_abc") == Path("sessions/sesn_abc.jsonl")
