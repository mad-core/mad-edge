"""Contract tests for the MCP inbound adapter mounted at ``/mcp`` (issue #32).

Two surfaces are exercised:

1. **Tool behaviour** — via the official in-memory MCP client session
   (``create_connected_server_and_client_session``) bound to the *same*
   ``app.state.mcp_server`` the HTTP app builds, so a tool call hits the
   same in-process use cases the REST routes hit. Every happy path has a
   negative twin (testing-heuristics rule 1).

2. **Transport mount** — a bounded route-level POST through the real
   ASGI stack (lifespan active so the StreamableHTTP session manager is
   running). The request is a single JSON-RPC ``initialize`` that
   completes immediately — never an open stream (rule 6, rule 8).

A drift test asserts the MCP tool schemas are the *same Pydantic models*
the FastAPI OpenAPI exposes, normalised across the ``$defs`` ↔
``components/schemas`` ref-prefix difference (rule 5).

The ``client`` fixture (conftest) injects a ``ScriptedLauncher`` and
redirects sessions/workspaces to tmp dirs — no real ``claude`` CLI or
GitHub (hard rule 5). Fakes live in ``tests/support`` (rule 3).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import CallToolResult

from mad.adapters.inbound.http.app import create_app
from mad.adapters.inbound.http.routes.sessions import (
    CreateSessionRequest,
    SessionDetailResponse,
)
from support.launchers import ScriptedLauncher

# --- helpers -----------------------------------------------------------------

_FILE_RESOURCE = {"type": "file", "mount_path": "/workspace/notes.md", "content": "hi"}
_AGENT = {"name": "a", "provider": "fake"}


@asynccontextmanager
async def _mcp_session(client: TestClient) -> AsyncIterator[ClientSession]:
    """Connect an in-memory MCP client to the app's own server instance."""
    async with create_connected_server_and_client_session(client.app.state.mcp_server) as session:
        yield session


def _dict_result(result: CallToolResult) -> dict[str, Any]:
    """Decode a dict-returning tool's JSON text payload."""
    assert result.isError is False, result.content
    return json.loads(result.content[0].text)  # type: ignore[union-attr]


async def _make_session(session: ClientSession) -> str:
    result = await session.call_tool(
        "mad_create_session",
        {"payload": {"agent": _AGENT, "resources": [_FILE_RESOURCE]}},
    )
    return _dict_result(result)["session_id"]


def _normalise_schema(node: Any) -> Any:
    """Normalise a JSON Schema for cross-dialect structural comparison.

    Two representation-only differences must not be read as drift:

    * **ref dialect** — FastAPI emits ``#/components/schemas/`` refs;
      Pydantic ``model_json_schema`` / MCP tool schemas emit ``$defs``
      refs. The prefix is rewritten to a single dialect.
    * **defaults** — Pydantic carries ``"default": ...`` for optional
      fields; FastAPI strips it from request-body components. Defaults
      are not representable on the OpenAPI side at all, so they are
      dropped from both sides before comparison. Field presence, type,
      ``$ref``, and ``required`` (the real contract) are preserved.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for k, v in node.items():
            if k == "default":
                continue
            if k == "$ref" and isinstance(v, str):
                out[k] = "#/$defs/" + v.rsplit("/", 1)[-1]
            else:
                out[k] = _normalise_schema(v)
        return out
    if isinstance(node, list):
        return [_normalise_schema(v) for v in node]
    return node


# --- tool: mad_create_session ------------------------------------------------


async def test_create_session_provisions_and_returns_created(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        body = _dict_result(
            await s.call_tool(
                "mad_create_session",
                {"payload": {"agent": _AGENT, "resources": [_FILE_RESOURCE]}},
            )
        )
    assert body["status"] == "created"


async def test_create_session_rejects_relative_mount_path(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_create_session",
            {
                "payload": {
                    "agent": _AGENT,
                    "resources": [{"type": "file", "mount_path": "notes.md", "content": "x"}],
                }
            },
        )
    assert result.isError is True
    assert "must be absolute" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_send_message --------------------------------------------------


async def test_send_message_accepts_and_returns_immediately(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_send_message",
                {"session_id": session_id, "payload": {"content": "go"}},
            )
        )
    assert body == {"status": "accepted"}


async def test_send_message_unknown_session_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_send_message",
            {"session_id": "sesn_missing", "payload": {"content": "go"}},
        )
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_list_sessions -------------------------------------------------


async def test_list_sessions_returns_live_session_with_raw_status(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool("mad_list_sessions", {})
    rows = result.structuredContent["result"]  # type: ignore[index]
    matched = [r for r in rows if r["session_id"] == session_id]
    assert matched == [
        {
            "session_id": session_id,
            "status": "created",
            "priority": 1,
            "created_at": matched[0]["created_at"],
            "updated_at": matched[0]["updated_at"],
        }
    ]


async def test_list_sessions_excludes_deleted_by_default(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        await s.call_tool("mad_delete_session", {"session_id": session_id})
        default = await s.call_tool("mad_list_sessions", {})
        with_deleted = await s.call_tool("mad_list_sessions", {"include_deleted": True})
    default_ids = [r["session_id"] for r in default.structuredContent["result"]]  # type: ignore[index]
    deleted_ids = [r["session_id"] for r in with_deleted.structuredContent["result"]]  # type: ignore[index]
    assert session_id not in default_ids
    assert session_id in deleted_ids


# --- tool: mad_get_session ---------------------------------------------------


async def test_get_session_returns_detail(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool("mad_get_session", {"session_id": session_id})
    assert result.structuredContent["status"] == "created"  # type: ignore[index]
    assert result.structuredContent["session_id"] == session_id  # type: ignore[index]


async def test_get_session_unknown_id_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool("mad_get_session", {"session_id": "sesn_missing"})
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_delete_session ------------------------------------------------


async def test_delete_session_destroys_and_returns_deleted(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(await s.call_tool("mad_delete_session", {"session_id": session_id}))
    assert body == {"status": "deleted", "session_id": session_id}


async def test_delete_session_unknown_id_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool("mad_delete_session", {"session_id": "sesn_missing"})
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool surface: exactly the five infrastructure tools, no event tools -----


async def test_tool_surface_is_exactly_the_five_session_tools(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        tools = await s.list_tools()
    names = sorted(t.name for t in tools.tools)
    assert names == [
        "mad_create_session",
        "mad_delete_session",
        "mad_get_session",
        "mad_list_sessions",
        "mad_send_message",
    ]


async def test_no_event_or_hook_tools_are_exposed(client: TestClient) -> None:
    """Negative twin of the surface test: telemetry stays on SSE (ADR-0004)."""
    async with _mcp_session(client) as s:
        tools = await s.list_tools()
    leaked = [t.name for t in tools.tools if "event" in t.name or "hook" in t.name]
    assert leaked == []


# --- transport: /mcp is mounted and speaks Streamable HTTP -------------------


def test_mcp_endpoint_is_mounted_and_initializes() -> None:
    """Bounded route-level check: a single JSON-RPC initialize completes.

    A ``with TestClient`` runs the lifespan so the StreamableHTTP session
    manager is active. The response is one framed JSON-RPC reply, not an
    open stream — the request terminates immediately (rule 6, rule 8).
    """
    with TestClient(create_app(launcher_factory=lambda _name: ScriptedLauncher())) as c:
        r = c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 200
    assert '"result"' in r.text


def test_unmounted_sibling_path_is_404() -> None:
    """Negative twin: proves the 200 above is the mount, not a catch-all."""
    with TestClient(create_app(launcher_factory=lambda _name: ScriptedLauncher())) as c:
        r = c.post("/mcp-not-real", json={})
    assert r.status_code == 404


# --- drift: MCP tool schemas ARE the FastAPI OpenAPI models (rule 5) ---------


@pytest.mark.asyncio
async def test_create_session_tool_schema_is_the_http_request_model(
    client: TestClient,
) -> None:
    async with _mcp_session(client) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    schema = tools["mad_create_session"].inputSchema
    defs = schema["$defs"]
    self_def = defs["CreateSessionRequest"]
    nested = {k: v for k, v in defs.items() if k != "CreateSessionRequest"}
    reconstructed = {**self_def, "$defs": nested}

    assert reconstructed == CreateSessionRequest.model_json_schema()


@pytest.mark.asyncio
async def test_get_session_tool_output_is_the_http_response_model(
    client: TestClient,
) -> None:
    async with _mcp_session(client) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
    assert tools["mad_get_session"].outputSchema == SessionDetailResponse.model_json_schema()


def test_mcp_tool_models_match_fastapi_openapi_components(client: TestClient) -> None:
    """The OpenAPI components and the MCP tool models describe one model.

    Normalising the ref dialect, the FastAPI OpenAPI component for
    ``CreateSessionRequest`` / ``SessionDetailResponse`` must equal the
    Pydantic model schema the MCP tools embed. If someone redefines a
    parallel model for MCP, this fails.
    """
    spec = client.get("/openapi.json").json()
    components = spec["components"]["schemas"]

    csr_component = _normalise_schema(components["CreateSessionRequest"])
    csr_model_self = _normalise_schema(
        {k: v for k, v in CreateSessionRequest.model_json_schema().items() if k != "$defs"}
    )
    assert csr_component == csr_model_self

    sdr_component = _normalise_schema(components["SessionDetailResponse"])
    sdr_model_self = _normalise_schema(
        {k: v for k, v in SessionDetailResponse.model_json_schema().items() if k != "$defs"}
    )
    assert sdr_component == sdr_model_self
