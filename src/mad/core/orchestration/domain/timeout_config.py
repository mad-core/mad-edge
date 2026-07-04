"""Agent-agnostic launcher timeout + precedence resolver (issue #61).

Replaces the two provider-specific timeout env vars
(``MAD_CLAUDE_CLI_TIMEOUT_S`` / ``MAD_OPENCODE_TIMEOUT_S``) with a single
operator knob, ``MAD_AGENT_TIMEOUT_S``, plus an optional per-session
override threaded from ``CreateSessionRequest.timeout_s``.

Mirrors the ``resolve_effective_model`` precedence helper (issue #55):
a pure function that takes every level as an explicit argument so it stays
framework-free and trivially testable.  Unlike model/effort there is no
deployment-config singleton — the operator default lives in the
``MAD_AGENT_TIMEOUT_S`` env var, read at the use-case boundary and passed
in here as ``env_timeout_s``.

Precedence (most specific wins):

1. per-session ``timeout_s`` (from the request)
2. ``MAD_AGENT_TIMEOUT_S`` env var (operator default)
3. hard-coded default: 600 s
"""

from __future__ import annotations

from mad.core.config.settings import AGENT_TIMEOUT_ENV, DEFAULT_AGENT_TIMEOUT_S, load_settings

#: Operator-facing env var that sets the global launcher timeout default.
#: Re-exported from the central settings module (issue #97) so the historical
#: import path keeps working.
AGENT_TIMEOUT_ENV_VAR = AGENT_TIMEOUT_ENV

__all__ = [
    "AGENT_TIMEOUT_ENV_VAR",
    "DEFAULT_AGENT_TIMEOUT_S",
    "env_timeout_s",
    "resolve_effective_timeout",
]


def resolve_effective_timeout(
    session_timeout_s: float | None,
    env_timeout_s: float | None,
    default_timeout_s: float = DEFAULT_AGENT_TIMEOUT_S,
) -> float:
    """Precedence: session > env > default.

    Returns the first non-None value, falling back to ``default_timeout_s``
    (600 s) when both the per-session override and the operator env default
    are unset.  Always returns a concrete float — every launcher run has a
    timeout, there is no "omit" sentinel (unlike model/effort).
    """
    for candidate in (session_timeout_s, env_timeout_s):
        if candidate is not None:
            return candidate
    return default_timeout_s


def env_timeout_s() -> float | None:
    """Read ``MAD_AGENT_TIMEOUT_S`` via the central settings module.

    Returns the parsed float, or ``None`` when the var is unset or empty so
    the resolver falls back to its hard-coded default.  A malformed value
    (non-numeric) also yields ``None`` rather than crashing a launch — the
    operator default silently reverts to 600 s. The read is delegated to
    :func:`~mad.core.config.settings.load_settings` (issue #97): the settings
    loader records ``source == "env"`` exactly when the variable was present
    and parsed, so this reader's historical contract is preserved.
    """
    timeout = load_settings().agent_timeout_s
    return timeout.value if timeout.source == "env" else None
