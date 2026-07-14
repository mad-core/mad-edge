"""Unit tests for the post-run auto-sync gate resolver (issue #109).

Precedence is task > session > env > ``True``. The load-bearing property — and
the whole reason the bug existed — is that ``False`` is a *value*, not an
absence: a task that manages its own named branch/PR sets ``auto_sync=False``
and that MUST beat a ``True`` at every less-specific level. A truthiness check
(``task_auto_sync or session_auto_sync or ...``) would silently fall through and
re-open the duplicate PR this issue is about, so each level is pinned with the
``False``-beats-``True`` case as its negative twin.

``env_auto_sync()`` is the env reader: it returns ``None`` unless the central
settings loader attributes the value to ``source == "env"``, so a malformed
``MAD_AUTO_SYNC`` leaves the safety net ON rather than disabling it.
"""

from __future__ import annotations

import pytest

from mad.core.orchestration.domain.auto_sync_config import (
    AUTO_SYNC_ENV_VAR,
    DEFAULT_AUTO_SYNC,
    env_auto_sync,
    resolve_effective_auto_sync,
)

# ---------------------------------------------------------------------------
# resolve_effective_auto_sync — precedence table
# ---------------------------------------------------------------------------


def test_all_levels_unset_keeps_the_safety_net_on() -> None:
    """Negative twin of every override case: with nothing set anywhere, auto-sync
    is ON. There is no ``None`` sentinel — every run either syncs or does not."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=None, env_auto_sync=None
    )
    assert result is True
    assert result == DEFAULT_AUTO_SYNC


def test_env_used_when_task_and_session_unset() -> None:
    """The operator default applies when neither task nor session overrides."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=None, env_auto_sync=False
    )
    assert result is False


def test_env_true_used_when_task_and_session_unset() -> None:
    """Negative twin of the env-False case: an env-level True is honoured too, so
    the False result above came from the env value and not from an unrelated
    fallthrough."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=None, env_auto_sync=True
    )
    assert result is True


def test_session_false_beats_env_true() -> None:
    """A session opting out wins over an env default that leaves it on."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=False, env_auto_sync=True
    )
    assert result is False


def test_session_true_beats_env_false() -> None:
    """Negative twin: a session opting IN wins over an env default that is off."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=True, env_auto_sync=False
    )
    assert result is True


def test_task_false_beats_session_true() -> None:
    """The core of issue #109: a task that manages its own branch/PR sets
    ``auto_sync=False`` and that MUST win over a session left at True.

    A truthiness-based resolver would fall through to the session's True here,
    fire the post-run publish, and open the duplicate PR this issue fixes.
    """
    result = resolve_effective_auto_sync(
        task_auto_sync=False, session_auto_sync=True, env_auto_sync=True
    )
    assert result is False


def test_task_true_beats_session_false() -> None:
    """Negative twin: a task opting IN wins over a session that opted out —
    precedence is symmetric, not a one-way "False sticks" latch."""
    result = resolve_effective_auto_sync(
        task_auto_sync=True, session_auto_sync=False, env_auto_sync=False
    )
    assert result is True


def test_task_false_beats_env_true_when_session_unset() -> None:
    """The task level short-circuits past an unset session straight over the env."""
    result = resolve_effective_auto_sync(
        task_auto_sync=False, session_auto_sync=None, env_auto_sync=True
    )
    assert result is False


def test_task_false_wins_over_the_hardcoded_default() -> None:
    """With env and session unset, an explicit task False still beats the
    built-in ``True`` — the default is a fallback, not a floor."""
    result = resolve_effective_auto_sync(
        task_auto_sync=False, session_auto_sync=None, env_auto_sync=None
    )
    assert result is False


def test_session_false_wins_over_the_hardcoded_default() -> None:
    """Same at the session level: ``False`` is a value, not an absence."""
    result = resolve_effective_auto_sync(
        task_auto_sync=None, session_auto_sync=False, env_auto_sync=None
    )
    assert result is False


def test_default_override_is_honoured_when_every_level_unset() -> None:
    """``default_auto_sync`` is injectable, and it only applies when every level
    above it is unset — the parameter is the last resort, not a veto."""
    assert (
        resolve_effective_auto_sync(
            task_auto_sync=None,
            session_auto_sync=None,
            env_auto_sync=None,
            default_auto_sync=False,
        )
        is False
    )
    assert (
        resolve_effective_auto_sync(
            task_auto_sync=True,
            session_auto_sync=None,
            env_auto_sync=None,
            default_auto_sync=False,
        )
        is True
    )


# ---------------------------------------------------------------------------
# env_auto_sync() reader
# ---------------------------------------------------------------------------


def test_env_auto_sync_reads_false_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator-set OFF value is surfaced to the resolver as False."""
    monkeypatch.setenv(AUTO_SYNC_ENV_VAR, "false")
    assert env_auto_sync() is False


def test_env_auto_sync_reads_true_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin: an operator-set ON value is surfaced as True, so the False
    above is the parsed value and not a blanket ``None``-to-falsey collapse."""
    monkeypatch.setenv(AUTO_SYNC_ENV_VAR, "true")
    assert env_auto_sync() is True


def test_env_auto_sync_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset yields None — NOT the default's ``True`` — so the resolver can tell
    "operator said on" apart from "operator said nothing"."""
    monkeypatch.delenv(AUTO_SYNC_ENV_VAR, raising=False)
    assert env_auto_sync() is None


@pytest.mark.parametrize("raw", ["maybe", "", "   ", "2"])
def test_env_auto_sync_is_none_when_malformed(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative twin of the ``"false"`` read: a typo must NOT be interpreted as an
    opt-out. It reports None, the resolver falls back to True, and the safety net
    stays on."""
    monkeypatch.setenv(AUTO_SYNC_ENV_VAR, raw)
    assert env_auto_sync() is None
