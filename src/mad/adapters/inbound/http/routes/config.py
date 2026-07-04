"""Read-only configuration endpoint — ``GET /v1/config`` (issue #107).

Exposes the server's **effective operational configuration** for operators and
agents (mad-cli ``status``/``versions`` integration, MCP introspection). Each
operational ``MAD_*`` tunable is returned as ``{value, source}`` where ``source``
is ``env`` when the operator set it and ``default`` when the built-in fallback is
in effect.

Credentials are NEVER returned — not even masked. Only presence booleans
(``set``/``unset``) appear, under ``credentials`` (hard rule 2). There is no
write path and no hot-reload (deliberately out of scope, issue #107): the
durable owner is the host-side ``.env`` managed by mad-cli.

The typed models below are the single source of truth for both this HTTP route
and the mirrored ``mad_get_config`` MCP tool (hard rule 13) — the tool imports
:func:`build_config_response` and returns the same :class:`ConfigResponse`.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from fastapi import APIRouter
from pydantic import BaseModel, Field

from mad.core.config.settings import Setting, Settings
from mad.core.config.use_cases.get_config import GetConfigUseCase

router = APIRouter(tags=["config"])

T = TypeVar("T")

Source = Literal["env", "default"]


class ConfigEntry(BaseModel, Generic[T]):
    """One resolved operational value plus where it was resolved from."""

    value: T = Field(..., description="The effective value in force for this process.")
    source: Source = Field(
        ...,
        description="`env` when set via the environment variable, `default` when the built-in "
        "fallback is in effect.",
    )


class CredentialsView(BaseModel):
    """Presence-only view of credential env vars — booleans, NEVER the values.

    A value (or even a masked value) is deliberately absent for every field
    here; only whether the variable is set to a non-blank value is reported
    (hard rule 2).
    """

    github_token: bool = Field(
        ..., description="`GITHUB_TOKEN` or `GH_TOKEN` is set to a non-blank value."
    )
    anthropic_api_key: bool = Field(..., description="`ANTHROPIC_API_KEY` is set.")
    claude_code_oauth_token: bool = Field(..., description="`CLAUDE_CODE_OAUTH_TOKEN` is set.")
    aws: bool = Field(..., description="`AWS_ACCESS_KEY_ID` is set.")


class ConfigResponse(BaseModel):
    """The server's effective operational configuration (read-only)."""

    agent_timeout_s: ConfigEntry[float] = Field(
        ...,
        description="Operator default agent wall-clock timeout, seconds (`MAD_AGENT_TIMEOUT_S`).",
    )
    sessions_dir: ConfigEntry[str] = Field(
        ..., description="Directory holding the per-session JSONL logs (`MAD_SESSIONS_DIR`)."
    )
    sessions_retention_days: ConfigEntry[int | None] = Field(
        ...,
        description="JSONL log retention TTL in days; `null` means retention disabled "
        "(`MAD_SESSIONS_RETENTION_DAYS`).",
    )
    sse_heartbeat_s: ConfigEntry[float] = Field(
        ..., description="SSE keepalive interval in seconds (`MAD_SSE_HEARTBEAT_S`)."
    )
    mcp_allowed_hosts: ConfigEntry[list[str]] = Field(
        ...,
        description="MCP DNS-rebinding Host allowlist; empty means protection off "
        "(`MAD_MCP_ALLOWED_HOSTS`).",
    )
    workspace_dir: ConfigEntry[str] = Field(
        ..., description="Base directory for per-session workspaces (`MAD_WORKSPACE_DIR`)."
    )
    hook_socket: ConfigEntry[str] = Field(
        ..., description="Unix Domain Socket path for hook ingestion (`MAD_HOOK_SOCKET`)."
    )
    claude_cli_bin: ConfigEntry[str | None] = Field(
        ...,
        description="Configured `claude` CLI binary override; `null` means auto-detect from PATH "
        "(`MAD_CLAUDE_CLI_BIN`).",
    )
    opencode_bin: ConfigEntry[str | None] = Field(
        ...,
        description="Configured `opencode` CLI binary override; `null` means auto-detect from PATH "
        "(`MAD_OPENCODE_BIN`).",
    )
    credentials: CredentialsView = Field(
        ..., description="Presence-only booleans for credential env vars (never the values)."
    )


def _entry(setting: Setting[T]) -> ConfigEntry[T]:
    return ConfigEntry[T](value=setting.value, source=setting.source)


def build_config_response(settings: Settings) -> ConfigResponse:
    """Map a core :class:`Settings` snapshot to the typed HTTP/MCP response.

    Credentials collapse to booleans here; the underlying secret strings are
    never read from ``settings`` (they are not present on it — hard rule 2).
    """
    return ConfigResponse(
        agent_timeout_s=_entry(settings.agent_timeout_s),
        sessions_dir=_entry(settings.sessions_dir),
        sessions_retention_days=_entry(settings.sessions_retention_days),
        sse_heartbeat_s=_entry(settings.sse_heartbeat_s),
        mcp_allowed_hosts=ConfigEntry[list[str]](
            value=list(settings.mcp_allowed_hosts.value),
            source=settings.mcp_allowed_hosts.source,
        ),
        workspace_dir=_entry(settings.workspace_dir),
        hook_socket=_entry(settings.hook_socket),
        claude_cli_bin=_entry(settings.claude_cli_bin),
        opencode_bin=_entry(settings.opencode_bin),
        credentials=CredentialsView(
            github_token=settings.credentials.github_token,
            anthropic_api_key=settings.credentials.anthropic_api_key,
            claude_code_oauth_token=settings.credentials.claude_code_oauth_token,
            aws=settings.credentials.aws,
        ),
    )


@router.get("/v1/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    """Return the server's effective operational configuration (read-only).

    Values reflect the process environment at request time; credentials are
    reported as presence booleans only, never as values (hard rule 2).
    """
    settings = GetConfigUseCase().execute()
    return build_config_response(settings)
