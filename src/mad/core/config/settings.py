"""Central, framework-free configuration surface for Mad (issue #97).

Historically every ``MAD_*`` tunable was read ad hoc with ``os.environ.get(...)``
scattered across adapters and one orchestration helper, with each call site
owning its own default, parsing, and "required vs optional" logic. That made the
surface impossible to introspect and let ``.env.example`` drift from the code.

This module is the single typed home for that surface. :func:`load_settings`
reads ``os.environ`` **once** and returns an immutable :class:`Settings`
snapshot; every former reader now delegates here instead of touching the
environment directly. Each operational value is wrapped in a :class:`Setting`
that records both the resolved value **and** whether it came from the
environment (``env``) or the built-in fallback (``default``) — exactly the shape
the read-only ``GET /v1/config`` surface serialises (issue #107).

Design constraints:

* **Framework-free (hard rule 4).** Only the standard library is used — no
  FastAPI, no ``subprocess``/``shutil``, no adapter imports — so this can live
  under ``mad.core`` and be imported by both the HTTP and MCP inbound adapters
  through the composition root. ``import-linter`` enforces the boundary.
* **Fresh-read semantics preserved.** Several tunables are, by contract, read on
  every call (e.g. ``MAD_SESSIONS_DIR`` and ``MAD_SSE_HEARTBEAT_S`` so an
  operator/test override is honoured at runtime, and ``MAD_AGENT_TIMEOUT_S``
  which participates in the per-session timeout precedence). Callers preserve
  that by invoking :func:`load_settings` at the point of use rather than caching
  a module-level snapshot — the centralisation is in *where* the parse lives,
  not in freezing the value at import time.
* **Token hygiene (hard rule 2).** Credentials are NEVER part of the value
  surface — only a boolean "is it set" flag. The actual clone token stays in
  ``mad.core.sessions.credentials`` (used for ``git clone`` then stripped); it
  never enters this module.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, TypeVar

T = TypeVar("T")

Source = Literal["env", "default"]

# ---------------------------------------------------------------------------
# Environment variable names — the single canonical spelling for each tunable.
# Adapters import these so a rename happens in exactly one place.
# ---------------------------------------------------------------------------

AGENT_TIMEOUT_ENV = "MAD_AGENT_TIMEOUT_S"
SESSIONS_DIR_ENV = "MAD_SESSIONS_DIR"
SESSIONS_RETENTION_DAYS_ENV = "MAD_SESSIONS_RETENTION_DAYS"
SSE_HEARTBEAT_ENV = "MAD_SSE_HEARTBEAT_S"
MCP_ALLOWED_HOSTS_ENV = "MAD_MCP_ALLOWED_HOSTS"
WORKSPACE_DIR_ENV = "MAD_WORKSPACE_DIR"
HOOK_SOCKET_ENV = "MAD_HOOK_SOCKET"
CLAUDE_CLI_BIN_ENV = "MAD_CLAUDE_CLI_BIN"
OPENCODE_BIN_ENV = "MAD_OPENCODE_BIN"
XDG_RUNTIME_DIR_ENV = "XDG_RUNTIME_DIR"

#: Consulted in order for the GitHub credential presence flag (mirrors
#: ``mad.core.sessions.credentials.GITHUB_TOKEN_ENV_VARS``).
GITHUB_TOKEN_ENV_VARS: tuple[str, ...] = ("GITHUB_TOKEN", "GH_TOKEN")
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
CLAUDE_CODE_OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 — env var NAME, not a secret
AWS_ACCESS_KEY_ID_ENV = "AWS_ACCESS_KEY_ID"

# ---------------------------------------------------------------------------
# Built-in fallbacks (the "default" source).
# ---------------------------------------------------------------------------

DEFAULT_AGENT_TIMEOUT_S = 600.0
DEFAULT_SESSIONS_DIR = "sessions"
DEFAULT_SSE_HEARTBEAT_S = 15.0
DEFAULT_HOOK_SOCKET_FALLBACK_BASE = "/tmp"  # noqa: S108 — matches the historical default


@dataclass(frozen=True)
class Setting(Generic[T]):
    """A resolved configuration value plus where it was resolved from.

    ``source`` is ``"env"`` only when the environment variable was present AND
    parsed to a value that the loader honoured; a missing, blank, or malformed
    variable that falls back reports ``"default"`` because the effective value
    IS the default.
    """

    value: T
    source: Source


@dataclass(frozen=True)
class CredentialFlags:
    """Presence-only view of the credentials Mad's environment may carry.

    Booleans exclusively — the underlying secret values are NEVER captured here
    (hard rule 2). Each flag is ``True`` when the corresponding variable is set
    to a non-blank value.
    """

    github_token: bool
    anthropic_api_key: bool
    claude_code_oauth_token: bool
    aws: bool


@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of Mad's effective operational configuration."""

    agent_timeout_s: Setting[float]
    sessions_dir: Setting[str]
    sessions_retention_days: Setting[int | None]
    sse_heartbeat_s: Setting[float]
    mcp_allowed_hosts: Setting[tuple[str, ...]]
    workspace_dir: Setting[str]
    hook_socket: Setting[str]
    claude_cli_bin: Setting[str | None]
    opencode_bin: Setting[str | None]
    credentials: CredentialFlags


def default_hook_socket_path(environ: dict[str, str] | None = None) -> str:
    """Compute the built-in hook-socket path (no ``MAD_HOOK_SOCKET`` override).

    ``$XDG_RUNTIME_DIR/mad/hooks.sock`` when the runtime dir is exported,
    otherwise ``/tmp/mad/hooks.sock``. Kept as a standalone helper because the
    launcher and the dual-uvicorn startup both need the pure default.
    """
    env = os.environ if environ is None else environ
    runtime_dir = env.get(XDG_RUNTIME_DIR_ENV)
    base = Path(runtime_dir) if runtime_dir else Path(DEFAULT_HOOK_SOCKET_FALLBACK_BASE)
    return str(base / "mad" / "hooks.sock")


def _is_set(raw: str | None) -> bool:
    """True when a credential-style variable carries a non-blank value."""
    return bool(raw and raw.strip())


def _resolve_agent_timeout(raw: str | None) -> Setting[float]:
    # ``if not raw`` treats an empty string as unset; a malformed numeric also
    # falls back — both preserving ``timeout_config.env_timeout_s`` semantics.
    if raw:
        try:
            return Setting(float(raw), "env")
        except ValueError:
            pass
    return Setting(DEFAULT_AGENT_TIMEOUT_S, "default")


def _resolve_sessions_dir(raw: str | None) -> Setting[str]:
    override = (raw or "").strip()
    if override:
        return Setting(override, "env")
    return Setting(DEFAULT_SESSIONS_DIR, "default")


def _resolve_retention_days(raw: str | None) -> Setting[int | None]:
    # Unset / non-integer / zero / negative all mean "retention disabled"
    # (issue #14) — the effective value equals the ``None`` default in each of
    # those cases, so the source is ``default``.
    if raw is not None:
        try:
            days = int(raw)
        except ValueError:
            days = 0
        if days > 0:
            return Setting(days, "env")
    return Setting(None, "default")


def _resolve_heartbeat(raw: str | None) -> Setting[float]:
    # Missing / unparseable / non-positive all fall back so a buffering proxy
    # cannot silently disable the SSE keepalive.
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return Setting(value, "env")
    return Setting(DEFAULT_SSE_HEARTBEAT_S, "default")


def _resolve_allowed_hosts(raw: str | None) -> Setting[tuple[str, ...]]:
    stripped = (raw or "").strip()
    if stripped:
        hosts = tuple(h.strip() for h in stripped.split(",") if h.strip())
        return Setting(hosts, "env")
    return Setting((), "default")


def _resolve_workspace_dir(raw: str | None) -> Setting[str]:
    # Operator value is used verbatim (no ``~``/``$VAR`` expansion); blank is
    # treated as unset. Default is ``~/mad``, dropping to the system temp dir
    # only when the home directory cannot be resolved.
    if raw and raw.strip():
        return Setting(raw, "env")
    try:
        return Setting(str(Path.home() / "mad"), "default")
    except RuntimeError:
        return Setting(str(Path(tempfile.gettempdir())), "default")


def _resolve_hook_socket(raw: str | None, environ: dict[str, str]) -> Setting[str]:
    # ``or`` fallthrough: an empty string is treated as unset.
    if raw:
        return Setting(raw, "env")
    return Setting(default_hook_socket_path(environ), "default")


def _resolve_optional_bin(raw: str | None) -> Setting[str | None]:
    # Stores only the configured override; the PATH lookup (``shutil.which``)
    # stays in the adapter, which cannot live in framework-free core. ``None``
    # means "auto-detect from PATH".
    if raw:
        return Setting(raw, "env")
    return Setting(None, "default")


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    """Read the environment once and return an immutable :class:`Settings`.

    Pass ``environ`` to resolve against an explicit mapping (used by tests);
    it defaults to the live ``os.environ``.
    """
    env = dict(os.environ) if environ is None else environ

    credentials = CredentialFlags(
        github_token=any(_is_set(env.get(name)) for name in GITHUB_TOKEN_ENV_VARS),
        anthropic_api_key=_is_set(env.get(ANTHROPIC_API_KEY_ENV)),
        claude_code_oauth_token=_is_set(env.get(CLAUDE_CODE_OAUTH_TOKEN_ENV)),
        aws=_is_set(env.get(AWS_ACCESS_KEY_ID_ENV)),
    )

    return Settings(
        agent_timeout_s=_resolve_agent_timeout(env.get(AGENT_TIMEOUT_ENV)),
        sessions_dir=_resolve_sessions_dir(env.get(SESSIONS_DIR_ENV)),
        sessions_retention_days=_resolve_retention_days(env.get(SESSIONS_RETENTION_DAYS_ENV)),
        sse_heartbeat_s=_resolve_heartbeat(env.get(SSE_HEARTBEAT_ENV)),
        mcp_allowed_hosts=_resolve_allowed_hosts(env.get(MCP_ALLOWED_HOSTS_ENV)),
        workspace_dir=_resolve_workspace_dir(env.get(WORKSPACE_DIR_ENV)),
        hook_socket=_resolve_hook_socket(env.get(HOOK_SOCKET_ENV), env),
        claude_cli_bin=_resolve_optional_bin(env.get(CLAUDE_CLI_BIN_ENV)),
        opencode_bin=_resolve_optional_bin(env.get(OPENCODE_BIN_ENV)),
        credentials=credentials,
    )
