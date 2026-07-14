"""Integration tests for GET /v1/config (issue #107).

The endpoint exposes the server's effective operational configuration:
every ``MAD_*`` tunable as ``{value, source}`` and credential *presence*
booleans — never credential values (hard rule 2).

Coverage (per the eight testing heuristics):
- Happy path: 200 with the typed shape, values reflecting the environment.
- Negative twins: ``source`` flips to ``default`` when the env is unset (rule 1),
  and the read-only route rejects a write with 405.
- OpenAPI contract test: the response model is declared and its credentials view
  carries booleans only (rule 5-style contract assertion).
- Secret-leak property test: a real ``GITHUB_TOKEN`` / ``ANTHROPIC_API_KEY`` in
  the process env NEVER appears in the response body (hard rule 2).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mad.adapters.inbound.http.app import create_app
from support.launchers import ScriptedLauncher

# Sentinel secrets — deliberately distinctive so a substring scan of the whole
# response body is unambiguous.
_FAKE_GITHUB_TOKEN = "ghp_leak_canary_GITHUB_do_not_emit"
_FAKE_ANTHROPIC_KEY = "sk-ant-leak_canary_ANTHROPIC_do_not_emit"

_CREDENTIAL_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "AWS_ACCESS_KEY_ID",
)

_TUNABLE_ENV_VARS = (
    "MAD_AGENT_TIMEOUT_S",
    "MAD_AUTO_SYNC",
    "MAD_SESSIONS_DIR",
    "MAD_SESSIONS_RETENTION_DAYS",
    "MAD_SSE_HEARTBEAT_S",
    "MAD_MCP_ALLOWED_HOSTS",
    "MAD_WORKSPACE_DIR",
    "MAD_HOOK_SOCKET",
    "MAD_CLAUDE_CLI_BIN",
    "MAD_OPENCODE_BIN",
)


@pytest.fixture
def config_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client over a fresh app with a fully cleared config environment.

    Every ``MAD_*`` tunable and credential var is deleted so each test starts
    from the built-in defaults and opts into ``env`` sources explicitly. No
    lifespan is run (plain ``TestClient``), which the read-only endpoint does
    not need.
    """
    for name in (*_TUNABLE_ENV_VARS, *_CREDENTIAL_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    return TestClient(create_app(launcher_factory=lambda _n: ScriptedLauncher()))


def test_get_config_returns_defaults_when_env_unset(config_client: TestClient) -> None:
    """Happy path: 200 with the typed shape; unset tunables read as defaults."""
    r = config_client.get("/v1/config")
    assert r.status_code == 200
    body = r.json()

    assert body["agent_timeout_s"] == {"value": 600.0, "source": "default"}
    assert body["auto_sync"] == {"value": True, "source": "default"}
    assert body["sessions_dir"] == {"value": "sessions", "source": "default"}
    assert body["sessions_retention_days"] == {"value": None, "source": "default"}
    assert body["sse_heartbeat_s"] == {"value": 15.0, "source": "default"}
    assert body["mcp_allowed_hosts"] == {"value": [], "source": "default"}
    assert body["claude_cli_bin"] == {"value": None, "source": "default"}
    assert body["credentials"] == {
        "github_token": False,
        "anthropic_api_key": False,
        "claude_code_oauth_token": False,
        "aws": False,
    }


def test_get_config_reflects_env_overrides(
    config_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin of the defaults test: a set env var reports source=env with
    the parsed value — the same fields that were `default` above flip to `env`."""
    monkeypatch.setenv("MAD_AGENT_TIMEOUT_S", "42")
    monkeypatch.setenv("MAD_SESSIONS_DIR", "/data/logs")
    monkeypatch.setenv("MAD_SESSIONS_RETENTION_DAYS", "7")
    monkeypatch.setenv("MAD_MCP_ALLOWED_HOSTS", "one.example, two.example")

    body = config_client.get("/v1/config").json()

    assert body["agent_timeout_s"] == {"value": 42.0, "source": "env"}
    assert body["sessions_dir"] == {"value": "/data/logs", "source": "env"}
    assert body["sessions_retention_days"] == {"value": 7, "source": "env"}
    assert body["mcp_allowed_hosts"] == {
        "value": ["one.example", "two.example"],
        "source": "env",
    }


def test_get_config_malformed_tunable_reports_default(
    config_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a malformed numeric tunable falls back to default, and the
    endpoint reports the fallback honestly as `default`, not `env`."""
    monkeypatch.setenv("MAD_AGENT_TIMEOUT_S", "not-a-number")

    body = config_client.get("/v1/config").json()

    assert body["agent_timeout_s"] == {"value": 600.0, "source": "default"}


def test_get_config_reports_auto_sync_disabled_from_env(
    config_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #109: the operator-wide auto-sync default is introspectable. With
    ``MAD_AUTO_SYNC=false`` exported, the endpoint reports the OFF value and
    attributes it to the environment."""
    monkeypatch.setenv("MAD_AUTO_SYNC", "false")

    body = config_client.get("/v1/config").json()

    assert body["auto_sync"] == {"value": False, "source": "env"}


def test_get_config_malformed_auto_sync_reports_on_default(
    config_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: a typo'd ``MAD_AUTO_SYNC`` does NOT silently disable the
    safety net. The endpoint reports the ON default and says so — ``source`` is
    ``default``, not ``env``, which is how an operator spots the typo."""
    monkeypatch.setenv("MAD_AUTO_SYNC", "maybe")

    body = config_client.get("/v1/config").json()

    assert body["auto_sync"] == {"value": True, "source": "default"}


def test_post_config_is_rejected_read_only(config_client: TestClient) -> None:
    """Negative twin: /v1/config is read-only — a write verb is 405, never a
    silent accept."""
    r = config_client.post("/v1/config", json={"agent_timeout_s": 1})
    assert r.status_code == 405


def test_get_config_openapi_declares_response_model(config_client: TestClient) -> None:
    """OpenAPI contract: the GET is declared, 200 references a named component,
    and the credentials view exposes booleans ONLY (no value field)."""
    spec = config_client.get("/openapi.json").json()
    assert "/v1/config" in spec["paths"]
    get_op = spec["paths"]["/v1/config"].get("get")
    assert get_op is not None

    schema = get_op["responses"]["200"]["content"]["application/json"]["schema"]
    ref = schema["$ref"]
    component_name = ref.rsplit("/", 1)[-1]
    component = spec["components"]["schemas"][component_name]
    props = component["properties"]
    assert "agent_timeout_s" in props
    assert "credentials" in props

    creds_name = props["credentials"]["$ref"].rsplit("/", 1)[-1]
    creds = spec["components"]["schemas"][creds_name]
    assert set(creds["properties"]) == {
        "github_token",
        "anthropic_api_key",
        "claude_code_oauth_token",
        "aws",
    }
    for field, subschema in creds["properties"].items():
        assert subschema["type"] == "boolean", f"{field} must be a boolean presence flag"


def test_get_config_reports_credential_presence_without_leaking_values(
    config_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard rule 2 property test: real secrets in the env are reported as
    presence booleans and their VALUES never appear anywhere in the body."""
    monkeypatch.setenv("GITHUB_TOKEN", _FAKE_GITHUB_TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_ANTHROPIC_KEY)

    r = config_client.get("/v1/config")
    assert r.status_code == 200
    body = r.json()

    assert body["credentials"]["github_token"] is True
    assert body["credentials"]["anthropic_api_key"] is True
    # The raw secret strings must not appear anywhere in the serialized body.
    assert _FAKE_GITHUB_TOKEN not in r.text
    assert _FAKE_ANTHROPIC_KEY not in r.text


def test_get_config_credentials_false_when_unset(config_client: TestClient) -> None:
    """Negative twin of the presence test: with no credential vars set, every
    flag is False (the fixture clears them)."""
    body = config_client.get("/v1/config").json()
    assert body["credentials"] == {
        "github_token": False,
        "anthropic_api_key": False,
        "claude_code_oauth_token": False,
        "aws": False,
    }
