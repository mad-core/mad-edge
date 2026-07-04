"""Unit tests for the central settings loader (issue #97).

``load_settings()`` is the single place ``os.environ`` is read for Mad's
operational tunables, so its per-field precedence, default fallbacks, and
``source`` attribution are load-bearing: the ``GET /v1/config`` surface renders
exactly what this loader resolves, and every former ad-hoc reader now delegates
here. Each field is pinned with its negative twin (testing-heuristics rule 1),
and every value assertion is paired with a ``source`` assertion (rule 4).

Tests pass an explicit ``environ`` mapping so they are hermetic — no process
env pollution — while exercising the exact code path the live ``os.environ``
read uses.
"""

from __future__ import annotations

from pathlib import Path

from mad.core.config.settings import (
    DEFAULT_AGENT_TIMEOUT_S,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_SSE_HEARTBEAT_S,
    CredentialFlags,
    Setting,
    default_hook_socket_path,
    load_settings,
)

# ---------------------------------------------------------------------------
# MAD_AGENT_TIMEOUT_S
# ---------------------------------------------------------------------------


def test_agent_timeout_reads_numeric_from_env() -> None:
    s = load_settings({"MAD_AGENT_TIMEOUT_S": "120"})
    assert s.agent_timeout_s == Setting(120.0, "env")


def test_agent_timeout_defaults_when_unset() -> None:
    s = load_settings({})
    assert s.agent_timeout_s == Setting(DEFAULT_AGENT_TIMEOUT_S, "default")
    assert s.agent_timeout_s.value == 600.0


def test_agent_timeout_falls_back_on_malformed() -> None:
    """Negative twin: a non-numeric value reverts to the default source."""
    s = load_settings({"MAD_AGENT_TIMEOUT_S": "not-a-number"})
    assert s.agent_timeout_s == Setting(600.0, "default")


def test_agent_timeout_empty_string_is_unset() -> None:
    s = load_settings({"MAD_AGENT_TIMEOUT_S": ""})
    assert s.agent_timeout_s.source == "default"


# ---------------------------------------------------------------------------
# MAD_SESSIONS_DIR
# ---------------------------------------------------------------------------


def test_sessions_dir_from_env() -> None:
    s = load_settings({"MAD_SESSIONS_DIR": "/data/mad-sessions"})
    assert s.sessions_dir == Setting("/data/mad-sessions", "env")


def test_sessions_dir_default_when_unset() -> None:
    s = load_settings({})
    assert s.sessions_dir == Setting(DEFAULT_SESSIONS_DIR, "default")
    assert s.sessions_dir.value == "sessions"


def test_sessions_dir_blank_is_unset() -> None:
    """Negative twin: whitespace-only is treated as unset, not an empty path."""
    s = load_settings({"MAD_SESSIONS_DIR": "   "})
    assert s.sessions_dir == Setting("sessions", "default")


# ---------------------------------------------------------------------------
# MAD_SESSIONS_RETENTION_DAYS
# ---------------------------------------------------------------------------


def test_retention_positive_int_from_env() -> None:
    s = load_settings({"MAD_SESSIONS_RETENTION_DAYS": "45"})
    assert s.sessions_retention_days == Setting(45, "env")


def test_retention_default_when_unset() -> None:
    s = load_settings({})
    assert s.sessions_retention_days == Setting(None, "default")


def test_retention_zero_disables() -> None:
    """Negative twin: an explicit 0 disables retention (None/default)."""
    s = load_settings({"MAD_SESSIONS_RETENTION_DAYS": "0"})
    assert s.sessions_retention_days == Setting(None, "default")


def test_retention_negative_disables() -> None:
    s = load_settings({"MAD_SESSIONS_RETENTION_DAYS": "-7"})
    assert s.sessions_retention_days == Setting(None, "default")


def test_retention_non_integer_disables() -> None:
    s = load_settings({"MAD_SESSIONS_RETENTION_DAYS": "thirty"})
    assert s.sessions_retention_days == Setting(None, "default")


# ---------------------------------------------------------------------------
# MAD_SSE_HEARTBEAT_S
# ---------------------------------------------------------------------------


def test_heartbeat_from_env() -> None:
    s = load_settings({"MAD_SSE_HEARTBEAT_S": "30"})
    assert s.sse_heartbeat_s == Setting(30.0, "env")


def test_heartbeat_default_when_unset() -> None:
    s = load_settings({})
    assert s.sse_heartbeat_s == Setting(DEFAULT_SSE_HEARTBEAT_S, "default")
    assert s.sse_heartbeat_s.value == 15.0


def test_heartbeat_non_positive_falls_back() -> None:
    """Negative twin: zero/negative degenerate to the default, never disabled."""
    assert load_settings({"MAD_SSE_HEARTBEAT_S": "0"}).sse_heartbeat_s == Setting(15.0, "default")
    assert load_settings({"MAD_SSE_HEARTBEAT_S": "-5"}).sse_heartbeat_s == Setting(15.0, "default")


def test_heartbeat_garbage_falls_back() -> None:
    s = load_settings({"MAD_SSE_HEARTBEAT_S": "fast"})
    assert s.sse_heartbeat_s == Setting(15.0, "default")


# ---------------------------------------------------------------------------
# MAD_MCP_ALLOWED_HOSTS
# ---------------------------------------------------------------------------


def test_allowed_hosts_parsed_and_trimmed() -> None:
    s = load_settings({"MAD_MCP_ALLOWED_HOSTS": " localhost , mad.example.com "})
    assert s.mcp_allowed_hosts == Setting(("localhost", "mad.example.com"), "env")


def test_allowed_hosts_default_empty_when_unset() -> None:
    """Negative twin: unset means protection OFF — an empty tuple, not None."""
    s = load_settings({})
    assert s.mcp_allowed_hosts == Setting((), "default")


def test_allowed_hosts_whitespace_only_is_unset() -> None:
    """A purely whitespace value is unset — protection stays OFF."""
    s = load_settings({"MAD_MCP_ALLOWED_HOSTS": "   "})
    assert s.mcp_allowed_hosts == Setting((), "default")


def test_allowed_hosts_comma_only_stays_env_with_empty_hosts() -> None:
    """Behaviour-preserving edge: a comma-only value has non-blank content, so
    the historical ``_transport_security`` enabled protection with an empty
    allowlist. The loader keeps that by reporting ``source == "env"`` even
    though the parsed host tuple is empty."""
    s = load_settings({"MAD_MCP_ALLOWED_HOSTS": "  , ,"})
    assert s.mcp_allowed_hosts == Setting((), "env")


# ---------------------------------------------------------------------------
# MAD_WORKSPACE_DIR
# ---------------------------------------------------------------------------


def test_workspace_dir_verbatim_from_env() -> None:
    # No ~/$VAR expansion — the operator value survives literally.
    s = load_settings({"MAD_WORKSPACE_DIR": "~/somewhere"})
    assert s.workspace_dir == Setting("~/somewhere", "env")


def test_workspace_dir_default_is_home_mad() -> None:
    """Negative twin: unset resolves to ~/mad with the default source."""
    s = load_settings({})
    assert s.workspace_dir.source == "default"
    assert s.workspace_dir.value == str(Path.home() / "mad")


def test_workspace_dir_blank_is_unset() -> None:
    s = load_settings({"MAD_WORKSPACE_DIR": "   "})
    assert s.workspace_dir.source == "default"


# ---------------------------------------------------------------------------
# MAD_HOOK_SOCKET
# ---------------------------------------------------------------------------


def test_hook_socket_explicit_override() -> None:
    s = load_settings({"MAD_HOOK_SOCKET": "/custom/mad.sock", "XDG_RUNTIME_DIR": "/run/user/1000"})
    assert s.hook_socket == Setting("/custom/mad.sock", "env")


def test_hook_socket_default_from_xdg() -> None:
    """Negative twin: no override falls back to the XDG-derived default."""
    s = load_settings({"XDG_RUNTIME_DIR": "/run/user/2000"})
    assert s.hook_socket == Setting("/run/user/2000/mad/hooks.sock", "default")


def test_hook_socket_default_tmp_without_xdg() -> None:
    s = load_settings({})
    assert s.hook_socket == Setting("/tmp/mad/hooks.sock", "default")


def test_hook_socket_empty_override_is_unset() -> None:
    s = load_settings({"MAD_HOOK_SOCKET": "", "XDG_RUNTIME_DIR": "/run/user/3000"})
    assert s.hook_socket == Setting("/run/user/3000/mad/hooks.sock", "default")


def test_default_hook_socket_path_helper_ignores_override() -> None:
    # The pure-default helper never consults MAD_HOOK_SOCKET.
    assert (
        default_hook_socket_path({"MAD_HOOK_SOCKET": "/x", "XDG_RUNTIME_DIR": "/run/user/9"})
        == "/run/user/9/mad/hooks.sock"
    )


# ---------------------------------------------------------------------------
# MAD_CLAUDE_CLI_BIN / MAD_OPENCODE_BIN
# ---------------------------------------------------------------------------


def test_claude_cli_bin_from_env() -> None:
    s = load_settings({"MAD_CLAUDE_CLI_BIN": "/opt/claude"})
    assert s.claude_cli_bin == Setting("/opt/claude", "env")


def test_claude_cli_bin_default_is_none() -> None:
    """Negative twin: unset means 'auto-detect from PATH' (None/default)."""
    s = load_settings({})
    assert s.claude_cli_bin == Setting(None, "default")


def test_opencode_bin_from_env() -> None:
    s = load_settings({"MAD_OPENCODE_BIN": "/opt/opencode"})
    assert s.opencode_bin == Setting("/opt/opencode", "env")


def test_opencode_bin_default_is_none() -> None:
    s = load_settings({})
    assert s.opencode_bin == Setting(None, "default")


# ---------------------------------------------------------------------------
# Credential presence flags — booleans only, never the value
# ---------------------------------------------------------------------------


def test_credentials_all_unset() -> None:
    s = load_settings({})
    assert s.credentials == CredentialFlags(
        github_token=False,
        anthropic_api_key=False,
        claude_code_oauth_token=False,
        aws=False,
    )


def test_credentials_github_via_github_token() -> None:
    s = load_settings({"GITHUB_TOKEN": "ghp_secret"})
    assert s.credentials.github_token is True


def test_credentials_github_via_gh_token_alias() -> None:
    s = load_settings({"GH_TOKEN": "ghp_secret"})
    assert s.credentials.github_token is True


def test_credentials_blank_token_is_unset() -> None:
    """Negative twin: an exported-but-blank var must not read as set."""
    s = load_settings({"GITHUB_TOKEN": "   "})
    assert s.credentials.github_token is False


def test_credentials_anthropic_oauth_and_aws() -> None:
    s = load_settings(
        {
            "ANTHROPIC_API_KEY": "sk-ant-x",
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-x",
            "AWS_ACCESS_KEY_ID": "AKIA-x",
        }
    )
    assert s.credentials.anthropic_api_key is True
    assert s.credentials.claude_code_oauth_token is True
    assert s.credentials.aws is True


def test_credential_secret_values_never_captured_in_settings() -> None:
    """Property test for hard rule 2: no secret string appears anywhere in the
    resolved Settings object — only booleans on the credentials view."""
    secret = "ghp_super_secret_value_do_not_leak"
    anthropic_secret = "sk-ant-do-not-leak"
    s = load_settings({"GITHUB_TOKEN": secret, "ANTHROPIC_API_KEY": anthropic_secret})

    assert s.credentials.github_token is True
    assert s.credentials.anthropic_api_key is True
    assert secret not in repr(s)
    assert anthropic_secret not in repr(s)
