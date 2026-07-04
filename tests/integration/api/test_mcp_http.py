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

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

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
from mad.core.orchestration.domain.task import Task
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


def _inject_queued(client: TestClient, session_id: str, *, content: str) -> Task:
    """Place a Task on the in-memory projection's queued list.

    The ``client`` fixture has no lifespan, so the dispatcher does not run
    and ``mad_enqueue_task`` (which only emits ``task.queued``) never feeds
    the projection. This injects the same surface the dispatcher would —
    identical to ``tests/integration/api/test_orchestration_http.py``.
    """
    projection = client.app.state.task_projection
    task = Task(
        task_id=uuid4(),
        session_id=session_id,
        content=content,
        scheduled_for="now",
        created_at=datetime(2026, 5, 8, tzinfo=UTC),
    )
    projection._queued.setdefault(session_id, []).append(task)
    return task


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


async def test_create_session_tool_threads_per_session_timeout(
    client: TestClient, fake_launcher: ScriptedLauncher
) -> None:
    """The mad_create_session tool mirrors timeout_s end-to-end (hard rule 13):
    a per-session timeout_s reaches the launcher when work is later dispatched."""
    fake_launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_create_session",
            {
                "payload": {
                    "agent": _AGENT,
                    "resources": [_FILE_RESOURCE],
                    "timeout_s": 33.0,
                }
            },
        )
        session_id = _dict_result(result)["session_id"]
        await s.call_tool(
            "mad_send_message",
            {"session_id": session_id, "payload": {"content": "go"}},
        )
    deadline = time.monotonic() + 5.0
    while len(fake_launcher.calls) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert fake_launcher.calls[0]["timeout_s"] == 33.0


async def test_create_session_tool_omits_timeout_falls_back_to_default(
    client: TestClient, fake_launcher: ScriptedLauncher, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative twin: no timeout_s on the tool payload and no env → 600 s default."""
    monkeypatch.delenv("MAD_AGENT_TIMEOUT_S", raising=False)
    fake_launcher.script(
        [
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
            [{"type": "session.status_idle", "stop_reason": "end_turn"}],
        ]
    )
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        await s.call_tool(
            "mad_send_message",
            {"session_id": session_id, "payload": {"content": "go"}},
        )
    deadline = time.monotonic() + 5.0
    while len(fake_launcher.calls) < 1 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert fake_launcher.calls[0]["timeout_s"] == 600.0


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


# --- tool surface: exactly the full request/response route set --------------


async def test_tool_surface_is_the_full_request_response_route_set(client: TestClient) -> None:
    """One MCP tool per request/response HTTP route (hard rule 13, ADR-0012).

    The list is kept explicit so an accidental tool removal (or a new
    route landing without its tool) breaks this test rather than passing
    silently. The only HTTP route deliberately absent is the streaming
    SSE surface ``GET /v1/events/stream``.
    """
    async with _mcp_session(client) as s:
        tools = await s.list_tools()
    names = sorted(t.name for t in tools.tools)
    assert names == sorted(
        [
            "mad_create_session",
            "mad_send_message",
            "mad_list_sessions",
            "mad_get_session",
            "mad_delete_session",
            "mad_cleanup_sessions",
            "mad_enqueue_task",
            "mad_list_tasks",
            "mad_cancel_task",
            "mad_set_session_dispatch_policy",
            "mad_clear_session_dispatch_policy",
            "mad_trigger_dispatch",
            "mad_get_deployment_dispatch_policy",
            "mad_set_deployment_dispatch_policy",
            "mad_set_session_priority",
            "mad_get_queue",
            "mad_create_workflow",
            "mad_get_workflow",
            "mad_query_events",
            "mad_list_provider_models",
            "mad_get_deployment_model",
            "mad_set_deployment_model",
            "mad_clear_deployment_model",
            "mad_get_deployment_effort",
            "mad_set_deployment_effort",
            "mad_clear_deployment_effort",
            "mad_get_config",
        ]
    )


async def test_streaming_and_hook_telemetry_are_not_tools(client: TestClient) -> None:
    """Negative twin of the surface test — the ADR-0012 carve-out.

    The streaming SSE surface (``GET /v1/events/stream``) and the hook
    firehose are operator telemetry, NOT request/response tools, so no
    tool name may contain ``stream`` or ``hook``. The *bounded* historical
    query ``GET /v1/events`` IS a legitimate tool (``mad_query_events``),
    so it is explicitly allowed here and must not be read as a leak.
    """
    async with _mcp_session(client) as s:
        tools = await s.list_tools()
    names = {t.name for t in tools.tools}
    leaked = [n for n in names if "stream" in n or "hook" in n]
    assert leaked == []
    assert "mad_query_events" in names


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


# =============================================================================
# Behavioural tests for the 10 new tools (ADR-0012). Each tool calls the same
# use case as its HTTP route, so the assertions mirror the HTTP integration
# suites (test_orchestration_http.py / test_dispatch_policy_http.py). Every
# happy path has a negative twin (rule 1); decoded results are pinned with
# ``==`` (rule 2). The ``client`` fixture has no lifespan, so the dispatcher
# never runs — queued state is injected the same way the HTTP suites do.
# =============================================================================


# --- tool: mad_cleanup_sessions ----------------------------------------------


async def test_cleanup_sessions_dry_run_reports_examined_and_would_delete(
    client: TestClient,
) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        # Cutoff captured AFTER creation: strictly later than the session's
        # ``updated_at`` (so it matches) yet not in the future (so the tool's
        # ``older_than is not valid`` guard does not fire).
        cutoff = datetime.now(UTC).isoformat()
        body = _dict_result(
            await s.call_tool(
                "mad_cleanup_sessions",
                {"payload": {"older_than": cutoff, "dry_run": True}},
            )
        )
    assert body["examined"] == 1
    assert body["would_delete"] == [session_id]
    assert body["deleted_session_ids"] == []


async def test_cleanup_sessions_future_cutoff_errors(client: TestClient) -> None:
    """Negative twin: an ``older_than`` in the future is invalid — the tool
    raises ``ValueError('older_than is not valid')`` (HTTP route returns 400).
    """
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    async with _mcp_session(client) as s:
        await _make_session(s)
        result = await s.call_tool(
            "mad_cleanup_sessions",
            {"payload": {"older_than": future, "dry_run": True}},
        )
    assert result.isError is True
    assert "older_than is not valid" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_enqueue_task --------------------------------------------------


async def test_enqueue_task_returns_task_id_and_queued_status(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_enqueue_task",
                {"session_id": session_id, "payload": {"content": "do the thing"}},
            )
        )
    assert body["session_id"] == session_id
    assert body["status"] == "queued"
    assert body["scheduled_for"] == "now"
    assert UUID(body["task_id"])  # parses as a real UUID


async def test_enqueue_task_unknown_session_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_enqueue_task",
            {"session_id": "sesn_missing", "payload": {"content": "x"}},
        )
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_list_tasks ----------------------------------------------------


async def test_list_tasks_returns_the_single_queued_task(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        task = _inject_queued(client, session_id, content="queued work")
        body = _dict_result(await s.call_tool("mad_list_tasks", {"session_id": session_id}))
    assert body["in_flight"] is None
    assert len(body["queued"]) == 1
    assert body["queued"][0]["task_id"] == str(task.task_id)
    assert body["queued"][0]["content"] == "queued work"


async def test_list_tasks_unknown_session_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool("mad_list_tasks", {"session_id": "sesn_missing"})
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_cancel_task ---------------------------------------------------


async def test_cancel_task_returns_cancelled_status_and_task_id(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        task = _inject_queued(client, session_id, content="will be cancelled")
        body = _dict_result(
            await s.call_tool(
                "mad_cancel_task",
                {"session_id": session_id, "task_id": str(task.task_id)},
            )
        )
    assert body == {"status": "cancelled", "task_id": str(task.task_id)}


async def test_cancel_task_unknown_task_errors(client: TestClient) -> None:
    """Negative twin: cancelling a task that was never queued raises
    (HTTP route returns 404)."""
    task_id = uuid4()
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool(
            "mad_cancel_task",
            {"session_id": session_id, "task_id": str(task_id)},
        )
    assert result.isError is True
    # Pin the contract: the failure names the missing task, not just "something broke".
    assert str(task_id) in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_set_session_dispatch_policy -----------------------------------


async def test_set_session_dispatch_policy_manual_round_trips(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_set_session_dispatch_policy",
                {"session_id": session_id, "payload": {"kind": "manual"}},
            )
        )
    assert body["session_id"] == session_id
    assert body["policy"] == {"kind": "manual"}


async def test_set_session_dispatch_policy_empty_windows_errors(client: TestClient) -> None:
    """Negative twin: a ``work_window`` with zero windows is rejected
    (HTTP route returns 422)."""
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool(
            "mad_set_session_dispatch_policy",
            {"session_id": session_id, "payload": {"kind": "work_window", "windows": []}},
        )
    assert result.isError is True
    # Pin the cause: the zero-windows validation, not an unrelated failure.
    assert "windows" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_clear_session_dispatch_policy ---------------------------------


async def test_clear_session_dispatch_policy_reinherits_immediate(client: TestClient) -> None:
    """After pinning manual then clearing, with no deployment default the
    session re-inherits the ``immediate`` fallback."""
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        await s.call_tool(
            "mad_set_session_dispatch_policy",
            {"session_id": session_id, "payload": {"kind": "manual"}},
        )
        body = _dict_result(
            await s.call_tool(
                "mad_clear_session_dispatch_policy",
                {"session_id": session_id},
            )
        )
    assert body["session_id"] == session_id
    assert body["inherited"] is True
    assert body["effective_policy"] == {"kind": "immediate"}


async def test_clear_session_dispatch_policy_unknown_session_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_clear_session_dispatch_policy",
            {"session_id": "sesn_missing"},
        )
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_trigger_dispatch ----------------------------------------------


async def test_trigger_dispatch_manual_drains_queued_tasks(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        await s.call_tool(
            "mad_set_session_dispatch_policy",
            {"session_id": session_id, "payload": {"kind": "manual"}},
        )
        _inject_queued(client, session_id, content="A")
        _inject_queued(client, session_id, content="B")
        body = _dict_result(await s.call_tool("mad_trigger_dispatch", {"session_id": session_id}))
    assert body == {"session_id": session_id, "drained": 2}


async def test_trigger_dispatch_immediate_mode_errors(client: TestClient) -> None:
    """Negative twin: in the default ``immediate`` mode the dispatcher
    already fires, so an explicit trigger is misconfiguration and raises
    (HTTP route returns 409)."""
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool("mad_trigger_dispatch", {"session_id": session_id})
    assert result.isError is True
    assert "immediate" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_get_deployment_dispatch_policy --------------------------------


async def test_get_deployment_dispatch_policy_unset_is_immediate(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        body = _dict_result(await s.call_tool("mad_get_deployment_dispatch_policy", {}))
    assert body == {"policy": {"kind": "immediate"}}


async def test_get_deployment_dispatch_policy_reflects_live_state(client: TestClient) -> None:
    """Twin / second-state: after a PUT-equivalent set to manual, GET reads
    live state, not a constant."""
    async with _mcp_session(client) as s:
        await s.call_tool(
            "mad_set_deployment_dispatch_policy",
            {"payload": {"kind": "manual"}},
        )
        body = _dict_result(await s.call_tool("mad_get_deployment_dispatch_policy", {}))
    assert body == {"policy": {"kind": "manual"}}


# --- tool: mad_set_deployment_dispatch_policy --------------------------------


async def test_set_deployment_dispatch_policy_manual_is_observable_on_get(
    client: TestClient,
) -> None:
    async with _mcp_session(client) as s:
        set_body = _dict_result(
            await s.call_tool(
                "mad_set_deployment_dispatch_policy",
                {"payload": {"kind": "manual"}},
            )
        )
        get_body = _dict_result(await s.call_tool("mad_get_deployment_dispatch_policy", {}))
    assert set_body == {"policy": {"kind": "manual"}}
    assert get_body == {"policy": {"kind": "manual"}}


async def test_set_deployment_dispatch_policy_empty_windows_errors(client: TestClient) -> None:
    """Negative twin: a malformed ``work_window`` (zero windows) is rejected
    (HTTP route returns 422)."""
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_set_deployment_dispatch_policy",
            {"payload": {"kind": "work_window", "windows": []}},
        )
    assert result.isError is True
    # Pin the cause: the zero-windows validation, not an unrelated failure.
    assert "windows" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_set_session_priority -------------------------------------------


async def test_set_session_priority_round_trips(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_set_session_priority",
                {"session_id": session_id, "payload": {"priority": 8}},
            )
        )
    assert body == {"session_id": session_id, "priority": 8}


async def test_set_session_priority_out_of_range_errors(client: TestClient) -> None:
    """Negative twin: 11 is outside [1, 10] and is rejected, never clamped
    (the HTTP route returns 422 for the same payload)."""
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        result = await s.call_tool(
            "mad_set_session_priority",
            {"session_id": session_id, "payload": {"priority": 11}},
        )
    assert result.isError is True
    # Pin the cause: the priority bound validation, not an unrelated failure.
    assert "priority" in result.content[0].text  # type: ignore[union-attr]


async def test_set_session_priority_unknown_session_errors(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        result = await s.call_tool(
            "mad_set_session_priority",
            {"session_id": "sesn_missing", "payload": {"priority": 5}},
        )
    assert result.isError is True
    assert "sesn_missing" in result.content[0].text  # type: ignore[union-attr]


# --- tool: mad_get_queue -------------------------------------------------------


async def test_get_queue_orders_ready_by_priority_desc(client: TestClient) -> None:
    """The queue tool surfaces true dispatch order: the priority-8 session's
    task leads the priority-1 session's task even though its session was
    created later (same ordering function the dispatcher uses)."""
    async with _mcp_session(client) as s:
        low_id = await _make_session(s)
        high_id = await _make_session(s)
        await s.call_tool(
            "mad_set_session_priority",
            {"session_id": high_id, "payload": {"priority": 8}},
        )
        low_task = _inject_queued(client, low_id, content="low")
        high_task = _inject_queued(client, high_id, content="high")
        body = _dict_result(await s.call_tool("mad_get_queue", {}))
    assert body["in_flight"] is None
    assert [e["task_id"] for e in body["ready"]] == [
        str(high_task.task_id),
        str(low_task.task_id),
    ]
    assert body["ready"][0]["priority"] == 8
    assert body["ready"][1]["priority"] == 1
    assert body["scheduled"] == []


async def test_get_queue_pending_unknown_session_errors(client: TestClient) -> None:
    """Negative twin: a queued task whose session is missing from the live
    index is an invariant violation — the tool fails loud (hard rule 7),
    never rendering a queue that silently omits work."""
    _inject_queued(client, "sesn_ghost", content="orphan")
    async with _mcp_session(client) as s:
        result = await s.call_tool("mad_get_queue", {})
    assert result.isError is True
    assert "sesn_ghost" in result.content[0].text  # type: ignore[union-attr]


async def test_get_queue_schedules_session_inheriting_manual_deployment_default(
    client: TestClient,
) -> None:
    """Effective-policy resolution (issue #45) in the queue tool: a session
    with NO per-session override inherits the manual deployment default, so
    its task is scheduled with the manual reason — exactly how the
    dispatcher would gate it. Twin of the ready-path test above."""
    async with _mcp_session(client) as s:
        await s.call_tool(
            "mad_set_deployment_dispatch_policy",
            {"payload": {"kind": "manual"}},
        )
        session_id = await _make_session(s)
        task = _inject_queued(client, session_id, content="gated")
        body = _dict_result(await s.call_tool("mad_get_queue", {}))
    assert body["ready"] == []
    assert [e["task_id"] for e in body["scheduled"]] == [str(task.task_id)]
    assert body["scheduled"][0]["reason"] == {"kind": "manual", "scheduled_for": None}


# --- tool: mad_query_events --------------------------------------------------


async def test_query_events_filters_to_the_session_created_event(client: TestClient) -> None:
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_query_events",
                {"session_id": session_id, "kind": "session.created"},
            )
        )
    assert len(body["events"]) == 1
    assert body["events"][0]["type"] == "session.created"
    assert body["events"][0]["session_id"] == session_id


async def test_query_events_unknown_kind_returns_empty_list(client: TestClient) -> None:
    """Negative twin: a kind that never happened is an empty result, NOT an
    error — the bounded query returns ``events == []``."""
    async with _mcp_session(client) as s:
        session_id = await _make_session(s)
        body = _dict_result(
            await s.call_tool(
                "mad_query_events",
                {"session_id": session_id, "kind": "nonexistent.kind"},
            )
        )
    assert body["events"] == []


# --- cross-check: tools share live state with each other and the dispatcher --


async def test_session_inherits_manual_deployment_default_so_trigger_drains(
    client: TestClient,
) -> None:
    """High-value integration: a session created with NO per-session policy
    inherits the deployment default. Setting the deployment default to manual
    (one tool), creating a session (another tool), then triggering (a third)
    drains the queue — proving the MCP tools share the same live deployment
    policy and projection. Mirrors the immediate-mode twin in
    ``test_trigger_dispatch_immediate_mode_errors``.
    """
    async with _mcp_session(client) as s:
        await s.call_tool(
            "mad_set_deployment_dispatch_policy",
            {"payload": {"kind": "manual"}},
        )
        session_id = await _make_session(s)
        _inject_queued(client, session_id, content="inherited drain")
        body = _dict_result(await s.call_tool("mad_trigger_dispatch", {"session_id": session_id}))
    assert body == {"session_id": session_id, "drained": 1}


# =============================================================================
# Behavioural tests for the 4 provider / deployment-model tools (FIX 5).
# Each happy path has a negative twin (rule 1); assertions are pinned with
# ``==`` to a single contract check (rule 2).
# =============================================================================


# --- tool: mad_list_provider_models ------------------------------------------


async def test_list_provider_models_contains_claude_cli_static_list(
    client: TestClient,
) -> None:
    """``mad_list_provider_models`` must return ``claude_cli`` with at least
    the three static fallback models ``opus``, ``sonnet``, ``haiku``."""
    async with _mcp_session(client) as s:
        body = _dict_result(await s.call_tool("mad_list_provider_models", {}))
    assert "claude_cli" in body["providers"]
    assert body["providers"]["claude_cli"] == ["opus", "sonnet", "haiku"]


async def test_list_provider_models_returns_all_registered_providers(
    client: TestClient,
) -> None:
    """Negative twin: the catalog must contain every registered provider key,
    not just ``claude_cli`` — a missing provider is a contract violation."""
    async with _mcp_session(client) as s:
        body = _dict_result(await s.call_tool("mad_list_provider_models", {}))
    assert set(body["providers"].keys()) >= {"claude_cli", "opencode"}


# --- tools: mad_set / mad_get / mad_clear_deployment_model -------------------


async def test_set_deployment_model_returns_the_new_value(client: TestClient) -> None:
    """``mad_set_deployment_model`` must echo back the model that was set."""
    async with _mcp_session(client) as s:
        body = _dict_result(
            await s.call_tool("mad_set_deployment_model", {"payload": {"model": "opus"}})
        )
    assert body == {"model": "opus"}


async def test_get_deployment_model_reflects_set_value(client: TestClient) -> None:
    """After a ``mad_set_deployment_model`` call, ``mad_get_deployment_model``
    must reflect the new value — the two tools share the same live config."""
    async with _mcp_session(client) as s:
        await s.call_tool("mad_set_deployment_model", {"payload": {"model": "opus"}})
        body = _dict_result(await s.call_tool("mad_get_deployment_model", {}))
    assert body == {"model": "opus"}


async def test_clear_deployment_model_returns_null(client: TestClient) -> None:
    """``mad_clear_deployment_model`` must return ``{"model": null}`` after
    clearing a previously set deployment model."""
    async with _mcp_session(client) as s:
        await s.call_tool("mad_set_deployment_model", {"payload": {"model": "opus"}})
        body = _dict_result(await s.call_tool("mad_clear_deployment_model", {}))
    assert body == {"model": None}


async def test_get_deployment_model_unset_returns_null(client: TestClient) -> None:
    """Negative twin: with no deployment model set, ``mad_get_deployment_model``
    returns ``{"model": null}`` — not an error, not an empty string."""
    async with _mcp_session(client) as s:
        body = _dict_result(await s.call_tool("mad_get_deployment_model", {}))
    assert body == {"model": None}


# --- tool: mad_get_config (issue #107) ---------------------------------------


async def test_get_config_tool_reports_credential_presence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mad_get_config`` returns the effective config with credential presence
    booleans; a set ``GITHUB_TOKEN`` reads as ``true``."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_mcp_canary_secret")
    async with _mcp_session(client) as s:
        body = _dict_result(await s.call_tool("mad_get_config", {}))
    assert body["credentials"]["github_token"] is True
    assert body["agent_timeout_s"]["source"] in {"env", "default"}


async def test_get_config_tool_never_leaks_secret_values(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hard rule 2 over the MCP surface: the raw token string must not appear
    anywhere in the tool's serialized result."""
    secret = "ghp_mcp_leak_canary_do_not_emit"
    monkeypatch.setenv("GITHUB_TOKEN", secret)
    async with _mcp_session(client) as s:
        result = await s.call_tool("mad_get_config", {})
    serialized = result.content[0].text  # type: ignore[union-attr]
    assert secret not in serialized


async def test_get_config_tool_matches_http_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parity (hard rule 13): the tool and GET /v1/config resolve the same env
    at call time and therefore return the same body."""
    monkeypatch.setenv("MAD_AGENT_TIMEOUT_S", "77")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    http_body = client.get("/v1/config").json()
    async with _mcp_session(client) as s:
        tool_body = _dict_result(await s.call_tool("mad_get_config", {}))
    assert tool_body == http_body
    assert tool_body["agent_timeout_s"] == {"value": 77.0, "source": "env"}
