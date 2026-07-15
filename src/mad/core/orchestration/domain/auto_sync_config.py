"""Post-run auto-sync toggle + precedence resolver (issue #109).

Auto-sync is a second, unrequested agent run Mad can fire after a primary run:
it publishes whatever uncommitted/unpushed work is left in the workspace (see
``mad.core.sessions.use_cases.auto_sync_prompt``). It can act as a safety net so
an ad-hoc session does not silently lose work — but it is **off by default**
(issue #109) and a session opts in when it wants that net.

The reason it is opt-in, not opt-out: auto-sync has no visibility into which
branch the primary task was told to use, so it defaults to ``mad/<session_id>``
and opens a **duplicate** PR alongside the real one. Because most tasks manage
their own named branch/PR, defaulting it on made that duplication the common
case. This module is the deterministic gate — a boolean resolved in Mad, *not*
an instruction we hope the agent obeys — that keeps it off unless asked for.

Mirrors the ``resolve_effective_timeout`` precedence helper (issue #61): a pure
function taking every level as an explicit argument, so it stays framework-free
and trivially testable. Like timeout (and unlike model/effort) the deployment
level is an env var read through the central settings module — there is no
mutable process-global singleton and no write endpoint; the operator default
lives in ``MAD_AUTO_SYNC`` and surfaces read-only on ``GET /v1/config``.

Precedence (most specific wins):

1. per-task ``auto_sync`` (from ``POST /v1/sessions/{id}/tasks``)
2. per-session ``auto_sync`` (from ``POST /v1/sessions``)
3. ``MAD_AUTO_SYNC`` env var (operator default)
4. hard-coded default: ``False`` (opt-in — do not publish unless asked)
"""

from __future__ import annotations

from mad.core.config.settings import AUTO_SYNC_ENV, DEFAULT_AUTO_SYNC, load_settings

#: Operator-facing env var that sets the deployment-wide auto-sync default.
#: Re-exported from the central settings module (issue #97) so callers have one
#: canonical spelling to import.
AUTO_SYNC_ENV_VAR = AUTO_SYNC_ENV

__all__ = [
    "AUTO_SYNC_ENV_VAR",
    "DEFAULT_AUTO_SYNC",
    "env_auto_sync",
    "resolve_effective_auto_sync",
]


def resolve_effective_auto_sync(
    task_auto_sync: bool | None,
    session_auto_sync: bool | None,
    env_auto_sync: bool | None,
    default_auto_sync: bool = DEFAULT_AUTO_SYNC,
) -> bool:
    """Precedence: task > session > env > default.

    Returns the first non-``None`` value, falling back to ``default_auto_sync``
    (``False``) when every level is unset. Always returns a concrete bool — every
    run either syncs or does not; there is no "omit" sentinel (unlike
    model/effort, where ``None`` means "let the provider decide").

    ``True`` is a *value*, not an absence: an explicit ``auto_sync=True`` at the
    task level wins over an ``auto_sync=False`` session, and either wins over the
    off-by-default fallback, which is the whole point of the knob. Hence the
    ``is not None`` test rather than a truthiness check — a truthiness check
    could never distinguish an explicit ``False`` from "unset".
    """
    for candidate in (task_auto_sync, session_auto_sync, env_auto_sync):
        if candidate is not None:
            return candidate
    return default_auto_sync


def env_auto_sync() -> bool | None:
    """Read ``MAD_AUTO_SYNC`` via the central settings module.

    Returns the parsed bool, or ``None`` when the var is unset, blank, or
    malformed, so the resolver falls back to its hard-coded default. The read is
    delegated to :func:`~mad.core.config.settings.load_settings` (issue #97),
    which records ``source == "env"`` exactly when the variable was present and
    recognised — so a typo'd value reports ``None`` here and auto-sync stays at
    its off-by-default state rather than being silently switched on.
    """
    auto_sync = load_settings().auto_sync
    return auto_sync.value if auto_sync.source == "env" else None
